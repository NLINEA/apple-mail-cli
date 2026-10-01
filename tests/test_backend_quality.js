/* Offline JXA tests with synthetic Mail objects. Never loads Apple Mail. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const crypto = require("node:crypto");
const os = require("node:os");
const backend = process.argv[2] || path.join(__dirname, "../skills/apple-mail-cli/scripts/mail_backend.js");
const source = fs.readFileSync(backend, "utf8");
let checks = 0;
const ownedTestRoot = fs.mkdtempSync(path.join(os.tmpdir(), "mailctl-send-logic-"));
process.on("exit", () => fs.rmSync(ownedTestRoot, { recursive: true, force: true }));

function nativeData(bytes) {
  return { bytes, length: bytes.length, base64EncodedStringWithOptions: () => bytes.toString("base64") };
}

function contextFor(payload, mail) {
  let applicationCalls = 0;
  const text = () => typeof payload === "string" ? payload : JSON.stringify(payload);
  const dollar = value => typeof value === "string" ? { dataUsingEncoding: () => nativeData(Buffer.from(value, "utf8")) } : value;
  Object.assign(dollar, {
    NSFileHandle: { fileHandleWithStandardInput: { get readDataToEndOfFile() { return text(); } } },
    NSString: { alloc: { initWithDataEncoding: value => value && value.bytes ? value.bytes.toString("utf8") : value } }, NSUTF8StringEncoding: 4,
    NSMutableData: { dataWithLength: size => { const buffer = Buffer.alloc(size); return { mutableBytes: buffer, base64EncodedStringWithOptions: () => buffer.toString("base64") }; } },
    CC_SHA256: (bytes, length, target) => crypto.createHash("sha256").update(bytes.subarray(0, length)).digest().copy(target),
    getuid: () => process.getuid(), NSHomeDirectory: () => ownedTestRoot,
    open: name => fs.openSync(name, fs.constants.O_RDONLY),
    fsync: descriptor => { fs.fsyncSync(descriptor); return 0; },
    close: descriptor => { fs.closeSync(descriptor); return 0; },
    NSData: { dataWithContentsOfFile: name => nativeData(fs.readFileSync(name)) },
    NSFileManager: { defaultManager: {
      attributesOfItemAtPathError(name) {
        try { const info = fs.lstatSync(name); return { NSFileType: info.isSymbolicLink() ? "NSFileTypeSymbolicLink" : info.isDirectory() ? "NSFileTypeDirectory" : "NSFileTypeRegular", NSFileOwnerAccountID: info.uid, NSFilePosixPermissions: info.mode & 511, NSFileSize: info.size }; }
        catch (_) { return null; }
      },
      createDirectoryAtPathWithIntermediateDirectoriesAttributesError(name, recursive, attributes) {
        try { fs.mkdirSync(name, { recursive, mode: attributes.NSFilePosixPermissions }); return true; }
        catch (_) { return false; }
      }
    } }
  });
  const ctx = vm.createContext({
    ObjC: { import() {}, bindFunction() {}, unwrap: value => value, deepUnwrap: value => value },
    $: dollar, Path: value => value,
    Application() { applicationCalls += 1; return mail; }
  });
  vm.runInContext(source, ctx);
  return { ctx, run: () => JSON.parse(ctx.run([])), calls: () => applicationCalls };
}

function prepareSend(fake, payload, files = []) {
  const root = fs.mkdtempSync(path.join(ownedTestRoot, "state-"));
  fs.chmodSync(root, 0o700);
  payload.attachment_evidence = files.map(file => ({ name: file.name, size_bytes: file.bytes.length, sha256: crypto.createHash("sha256").update(file.bytes).digest("hex") }));
  payload.attachments = files.map(() => "pending");
  const intent = fake.ctx.sendIntent(payload);
  const digest = crypto.createHash("sha256").update(fake.ctx.canonicalJson(intent), "utf8").digest("hex");
  payload.confirm_send = digest;
  const directory = path.join(root, digest);
  fs.mkdirSync(directory, { mode: 0o700 });
  const record = path.join(directory, "attempt.json");
  fs.writeFileSync(record, JSON.stringify({ schema: "mailctl-send-attempt/v1", digest, state: "in_flight" }), { mode: 0o600 });
  payload.attachments = files.map((file, index) => {
    const parent = path.join(directory, "attachments", String(index));
    fs.mkdirSync(parent, { recursive: true, mode: 0o700 });
    const name = path.join(parent, file.name);
    fs.writeFileSync(name, file.bytes, { mode: 0o400 });
    return name;
  });
  fake.ctx.sendStateRoot = () => root;
  return { root, directory, record, digest };
}

function syntheticSendPayload() {
  return { command: "send", sender: "sender@example.test", to: ["recipient@example.test"], cc: ["copy@example.test"], bcc: ["private-copy@example.test"], subject: "香港 café", body: "Final body\n\n", attachments: [] };
}

const ping = contextFor({ command: "ping", synthetic: "香港 café" });
assert.equal(ping.run().ok, true);
assert.equal(ping.calls(), 0);
checks += 1;

for (const payload of ["invalid JSON", "null", "[]", { command: "unknown" },
                       { command: "send" }, { command: "send", confirm_send: "true" },
                       { command: "send", confirm_send: true },
                       { command: "trash" }, { command: "trash", confirm_trash: 1 },
                       { command: "mark" }, { command: "flag" }, { command: "move" }]) {
  const fake = contextFor(payload);
  assert.equal(fake.run().ok, false);
  assert.equal(fake.calls(), 0, "invalid or unconfirmed requests must not load Mail");
  checks += 1;
}

function account(name, address, enabled = true) {
  return { name: () => name, emailAddresses: () => [address], enabled: () => enabled };
}
for (const names of [["Work", "WORK"], ["Other"]]) {
  const fake = contextFor({}, { accounts: () => names.map(n => account(n, "sender@example.test")), inbox: () => ({}) });
  assert.throws(() => fake.ctx.resolveMailbox({ accounts: () => names.map(n => account(n, "sender@example.test")), inbox: () => ({}) }, "INBOX", "Work"));
  checks += 1;
}

const unique = { accounts: () => [account("工作", "sender@example.test")], inbox: () => ({}) };
const uniqueCtx = contextFor({}, unique);
assert.equal(uniqueCtx.ctx.resolveMailbox(unique, "INBOX", "工作").accountFilter, "工作");
checks += 1;

const duplicateBox = { name: () => "Archive" };
const ambiguousCustom = { accounts: () => [
  { name: () => "One", mailboxes: () => [duplicateBox, duplicateBox] },
  { name: () => "Two", mailboxes: () => [duplicateBox] }
] };
const ambiguousCtx = contextFor({}, ambiguousCustom);
assert.throws(() => ambiguousCtx.ctx.resolveMailbox(ambiguousCustom, "Archive", ""));
checks += 1;

for (const accounts of [[], [account("Disabled", "sender@example.test", false)],
                       [account("One", "sender@example.test"), account("Two", "sender@example.test")]]) {
  let created = 0;
  const fake = contextFor({ command: "compose", to: ["recipient@example.test"], sender: "sender@example.test", subject: "Synthetic", body: "合成" },
    { accounts: () => accounts, OutgoingMessage() { created += 1; throw new Error("unexpected creation"); } });
  assert.equal(fake.run().ok, false);
  assert.equal(created, 0, "unavailable or ambiguous senders must fail before draft creation");
  checks += 1;
}
for (const command of ["compose", "send"]) {
  let created = 0;
  let sends = 0;
  const outgoing = { id: () => 99, toRecipients: [], ccRecipients: [], bccRecipients: [], send() { sends += 1; return true; } };
  const mail = { accounts: () => [account("Work", "sender@example.test")],
                 OutgoingMessage() { created += 1; return outgoing; },
                 outgoingMessages: [], ToRecipient: value => value };
  const payload = { command, sender: "sender@example.test", to: ["recipient@example.test"], cc: [], bcc: [], subject: "Synthetic", body: "合成", attachments: [] };
  const fake = contextFor(payload, mail);
  if (command === "send") prepareSend(fake, payload);
  const result = fake.run();
  assert.equal(result.ok, true);
  assert.equal(created, 1);
  assert.equal(sends, command === "send" ? 1 : 0);
  assert.equal(command === "send" ? result.data.delivery_confirmed : result.data.sent, false);
  checks += 1;
}
for (const content of [() => { throw new Error("synthetic body access denied"); }, () => "", () => "香港 readable body"]) {
  const message = { id: () => 42, subject: () => "Synthetic", content };
  const messages = () => [message];
  messages.byId = () => message;
  const mail = { inbox: () => ({ messages }) };
  const fake = contextFor({ command: "read", message_id: 42 }, mail);
  const result = fake.run();
  if (content.toString().includes("throw")) {
    assert.equal(result.ok, false, "body read failure must not become a successful empty read");
    assert.equal(result.data, undefined);
  } else {
    assert.equal(result.ok, true);
    assert.equal(result.data.content, content());
    assert.equal(result.data.content_truncated, false);
  }
  checks += 1;
}
const unreadable = { id: () => 42, subject: () => "Synthetic", content() { throw new Error("synthetic body access denied"); } };
const unreadableList = contextFor({ command: "list", query: "needle", search_body: true }, { inbox: () => ({ messages: () => [unreadable] }) });
assert.equal(unreadableList.run().ok, false, "body search must not silently skip inaccessible content");
checks += 1;

for (const field of ["sender", "to", "cc", "bcc", "subject", "body"]) {
  const payload = syntheticSendPayload();
  const fake = contextFor(payload);
  prepareSend(fake, payload);
  payload[field] = Array.isArray(payload[field]) ? ["other@example.test"] : "Changed";
  assert.equal(fake.run().ok, false, "confirmed " + field + " drift must fail");
  assert.equal(fake.calls(), 0);
  checks += 1;
}
for (const defect of ["bytes", "size", "path", "record", "permissions"]) {
  const payload = syntheticSendPayload();
  const fake = contextFor(payload);
  const state = prepareSend(fake, payload, [{ name: "approved.pdf", bytes: Buffer.from("approved") }]);
  const snapshot = payload.attachments[0];
  if (defect === "bytes" || defect === "size") {
    fs.chmodSync(snapshot, 0o600);
    fs.writeFileSync(snapshot, defect === "bytes" ? "altered!" : "longer bytes");
    fs.chmodSync(snapshot, 0o400);
  }
  if (defect === "path") payload.attachments[0] = state.record;
  if (defect === "record") fs.writeFileSync(state.record, JSON.stringify({ schema: "mailctl-send-attempt/v1", digest: state.digest, state: "accepted_by_mail" }));
  if (defect === "permissions") fs.chmodSync(state.root, 0o755);
  assert.equal(fake.run().ok, false, defect + " must fail before Mail loads");
  assert.equal(fake.calls(), 0);
  checks += 1;
}
for (const outcome of [true, false, "throw"]) {
  let sends = 0;
  const payload = syntheticSendPayload();
  const outgoing = { toRecipients: [], ccRecipients: [], bccRecipients: [], content: { attachments: [] }, send() { sends += 1; if (outcome === "throw") throw new Error("synthetic partial-send error"); return outcome; } };
  const mail = { accounts: () => [account("Work", "sender@example.test")], OutgoingMessage: () => outgoing, outgoingMessages: [], ToRecipient: value => value, CcRecipient: value => value, BccRecipient: value => value, Attachment: value => value };
  const fake = contextFor(payload, mail);
  prepareSend(fake, payload, [{ name: "approved.pdf", bytes: Buffer.from("approved") }]);
  const first = fake.run();
  assert.equal(first.ok, outcome === true);
  if (first.ok) {
    assert.equal(first.data.submission_accepted, true);
    assert.equal(first.data.delivery_confirmed, false);
    assert.equal(first.data.sent, undefined);
  }
  assert.equal(fake.run().ok, false, "raw backend replay must not reissue");
  assert.equal(sends, 1);
  assert.equal(fake.calls(), 1);
  checks += 1;
}
console.log(JSON.stringify({ synthetic_backend_checks: checks, ok: true, mail_loaded: false }));
