/* Apple Mail JXA backend. Invoke through mailctl.py. */

function asText(value) {
  if (value === null || value === undefined) return "";
  return String(value);
}

function lower(value) {
  return asText(value).toLowerCase();
}

function callOr(object, property, fallback) {
  try {
    return object[property]();
  } catch (error) {
    return fallback;
  }
}

function isoDate(value) {
  try {
    return value ? value.toISOString() : null;
  } catch (error) {
    try {
      return value ? new Date(value).toISOString() : null;
    } catch (ignored) {
      return null;
    }
  }
}

function getAccounts(Mail) {
  return Mail.accounts();
}

function findAccount(Mail, wanted) {
  var accounts = getAccounts(Mail);
  var needle = lower(wanted);
  var matches = [];
  for (var i = 0; i < accounts.length; i += 1) {
    if (lower(callOr(accounts[i], "name", "")) === needle) matches.push(accounts[i]);
  }
  if (matches.length === 1) return matches[0];
  if (matches.length > 1) throw new Error("More than one account is named '" + wanted + "'.");
  throw new Error("Mail account not found: " + wanted);
}

function childByName(collection, wanted) {
  var boxes = collection();
  var needle = lower(wanted);
  var matches = [];
  for (var i = 0; i < boxes.length; i += 1) {
    if (lower(callOr(boxes[i], "name", "")) === needle) matches.push(boxes[i]);
  }
  if (matches.length === 1) return matches[0];
  if (matches.length > 1) throw new Error("Mailbox name is ambiguous: " + wanted);
  return null;
}

function mailboxInAccount(account, path) {
  var parts = asText(path).split("/").filter(function (part) { return part.length > 0; });
  if (parts.length === 0) throw new Error("Mailbox path cannot be empty.");
  var collection = account.mailboxes;
  var current = null;
  for (var i = 0; i < parts.length; i += 1) {
    current = childByName(collection, parts[i]);
    if (!current) {
      throw new Error("Mailbox not found in account '" + callOr(account, "name", "") + "': " + path);
    }
    collection = current.mailboxes;
  }
  return current;
}

function specialMailbox(Mail, path) {
  var key = asText(path || "INBOX").toUpperCase();
  if (key === "INBOX" || key === "IN") return Mail.inbox();
  if (key === "SENT") return Mail.sentMailbox();
  if (key === "DRAFTS" || key === "DRAFT") return Mail.draftsMailbox();
  if (key === "TRASH" || key === "BIN") return Mail.trashMailbox();
  if (key === "JUNK" || key === "SPAM") return Mail.junkMailbox();
  return null;
}

function resolveMailbox(Mail, path, accountName) {
  var special = specialMailbox(Mail, path);
  if (special) {
    var exactAccount = accountName ? findAccount(Mail, accountName) : null;
    return { box: special, path: asText(path || "INBOX").toUpperCase(), accountFilter: exactAccount ? asText(callOr(exactAccount, "name", "")) : "" };
  }
  if (accountName) {
    var account = findAccount(Mail, accountName);
    return { box: mailboxInAccount(account, path), path: asText(path), accountFilter: asText(callOr(account, "name", "")) };
  }
  var accounts = getAccounts(Mail);
  var matches = [];
  for (var i = 0; i < accounts.length; i += 1) {
    try {
      matches.push({ box: mailboxInAccount(accounts[i], path), account: asText(callOr(accounts[i], "name", "")) });
    } catch (error) {
      // Only a missing path is a reason to continue across accounts.
      // Ambiguity or an automation failure must not silently select another account.
      if (asText(error.message).indexOf("Mailbox not found in account ") !== 0) throw error;
    }
  }
  if (matches.length === 1) return { box: matches[0].box, path: asText(path), accountFilter: matches[0].account };
  if (matches.length === 0) throw new Error("Mailbox not found: " + path);
  throw new Error("Mailbox exists in multiple accounts; pass --account: " + path);
}

function messageContext(message, fallbackPath) {
  var result = { account: "", mailbox: asText(fallbackPath) };
  try {
    var box = message.mailbox();
    result.mailbox = asText(box.name());
    result.account = asText(box.account().name());
  } catch (ignored) {
    // Aggregate mailboxes may not expose an account.
  }
  return result;
}

function accountMatches(message, wanted) {
  return !wanted || lower(messageContext(message, "").account) === lower(wanted);
}

function recipientList(message, property) {
  var result = [];
  try {
    var values = message[property]();
    for (var i = 0; i < values.length; i += 1) {
      result.push({ name: asText(callOr(values[i], "name", "")), address: asText(callOr(values[i], "address", "")) });
    }
  } catch (ignored) {
    // Some mailbox types omit recipients.
  }
  return result;
}

function attachmentList(message) {
  var result = [];
  try {
    var values = message.mailAttachments();
    for (var i = 0; i < values.length; i += 1) {
      result.push({
        id: asText(callOr(values[i], "id", "")),
        name: asText(callOr(values[i], "name", "")),
        mime_type: asText(callOr(values[i], "mimeType", "")),
        size_bytes: Number(callOr(values[i], "fileSize", 0)),
        downloaded: Boolean(callOr(values[i], "downloaded", false))
      });
    }
  } catch (ignored) {
    return null; // Unavailable is not proof of zero attachments.
  }
  return result;
}

function messageSummary(message, fallbackPath) {
  var context = messageContext(message, fallbackPath);
  return {
    id: Number(callOr(message, "id", 0)),
    message_id: asText(callOr(message, "messageId", "")),
    subject: asText(callOr(message, "subject", "")),
    sender: asText(callOr(message, "sender", "")),
    date_received: isoDate(callOr(message, "dateReceived", null)),
    date_sent: isoDate(callOr(message, "dateSent", null)),
    unread: !Boolean(callOr(message, "readStatus", false)),
    flagged: Boolean(callOr(message, "flaggedStatus", false)),
    size_bytes: Number(callOr(message, "messageSize", 0)),
    account: context.account,
    mailbox: context.mailbox
  };
}

function messageById(location, id) {
  var message = location.box.messages.byId(Number(id));
  if (Number(message.id()) !== Number(id)) throw new Error("Message not found: " + id);
  if (!accountMatches(message, location.accountFilter)) {
    throw new Error("Message " + id + " is not in account '" + location.accountFilter + "'.");
  }
  return message;
}

function walkMailboxes(collection, account, prefix, depth, output) {
  var boxes = collection();
  for (var i = 0; i < boxes.length; i += 1) {
    var name = asText(callOr(boxes[i], "name", ""));
    var path = prefix ? prefix + "/" + name : name;
    output.push({ account: account, name: name, path: path, unread_count: Number(callOr(boxes[i], "unreadCount", 0)) });
    if (depth > 1) walkMailboxes(boxes[i].mailboxes, account, path, depth - 1, output);
  }
}

function accountsCommand(Mail) {
  var accounts = getAccounts(Mail);
  var output = [];
  for (var i = 0; i < accounts.length; i += 1) {
    output.push({
      id: asText(callOr(accounts[i], "id", "")),
      name: asText(callOr(accounts[i], "name", "")),
      full_name: asText(callOr(accounts[i], "fullName", "")),
      email_addresses: callOr(accounts[i], "emailAddresses", []),
      enabled: Boolean(callOr(accounts[i], "enabled", false)),
      account_type: asText(callOr(accounts[i], "accountType", ""))
    });
  }
  return output;
}

function mailboxesCommand(Mail, payload) {
  var output = [];
  var depth = Math.max(1, Math.min(Number(payload.depth || 12), 30));
  var accounts = payload.account ? [findAccount(Mail, payload.account)] : getAccounts(Mail);
  for (var i = 0; i < accounts.length; i += 1) {
    walkMailboxes(accounts[i].mailboxes, asText(callOr(accounts[i], "name", "")), "", depth, output);
  }
  return output;
}

function listCommand(Mail, payload) {
  var location = resolveMailbox(Mail, payload.mailbox || "INBOX", payload.account || "");
  var messages = location.box.messages();
  var scanLimit = Math.max(1, Math.min(Number(payload.scan_limit || 500), 10000));
  var resultLimit = Math.max(1, Math.min(Number(payload.limit || 25), 1000));
  var query = lower(payload.query || "");
  var since = payload.since ? new Date(payload.since).getTime() : null;
  var before = payload.before ? new Date(payload.before).getTime() : null;
  var output = [];
  var scanned = Math.min(messages.length, scanLimit);

  for (var i = 0; i < scanned; i += 1) {
    var message = messages[i];
    if (!accountMatches(message, location.accountFilter)) continue;
    var item = messageSummary(message, location.path);
    if (payload.unread_only && !item.unread) continue;
    if (payload.flagged_only && !item.flagged) continue;
    var received = item.date_received ? new Date(item.date_received).getTime() : null;
    if (since !== null && (received === null || received < since)) continue;
    if (before !== null && (received === null || received >= before)) continue;
    if (query) {
      var haystack = lower(item.subject) + "\n" + lower(item.sender);
      if (payload.search_body) haystack += "\n" + lower(message.content());
      if (haystack.indexOf(query) === -1) continue;
    }
    output.push(item);
  }

  output.sort(function (left, right) {
    return asText(right.date_received).localeCompare(asText(left.date_received));
  });

  return {
    messages: output.slice(0, resultLimit),
    returned: Math.min(output.length, resultLimit),
    matched_in_scan: output.length,
    scanned: scanned,
    available_in_mailbox: messages.length,
    truncated: messages.length > scanLimit || output.length > resultLimit,
    mailbox: location.path,
    account_filter: location.accountFilter || null
  };
}

function readCommand(Mail, payload, resolvedLocation) {
  var location = resolvedLocation || resolveMailbox(Mail, payload.mailbox || "INBOX", payload.account || "");
  var message = messageById(location, payload.message_id);
  var item = messageSummary(message, location.path);
  // Body access is required for a complete read. A failed read must propagate
  // through run's error envelope, rather than masquerading as an empty body.
  var content = asText(message.content());
  var maxChars = Math.max(1000, Math.min(Number(payload.max_chars || 20000), 200000));
  item.reply_to = asText(callOr(message, "replyTo", ""));
  item.to = recipientList(message, "toRecipients");
  item.cc = recipientList(message, "ccRecipients");
  item.bcc = recipientList(message, "bccRecipients");
  item.attachments = attachmentList(message);
  item.content = content.slice(0, maxChars);
  item.content_truncated = content.length > maxChars;
  item.headers = asText(callOr(message, "allHeaders", "")).slice(0, 65536);
  return item;
}

function readBatchCommand(Mail, payload) {
  var ids = payload.message_ids;
  if (!Array.isArray(ids) || ids.length < 1 || ids.length > 20) throw new Error("Batch requires 1 to 20 message IDs.");
  var seen = {};
  for (var j = 0; j < ids.length; j += 1) {
    if (!Number.isSafeInteger(ids[j]) || ids[j] <= 0 || seen[ids[j]]) throw new Error("Batch IDs must be unique positive integers.");
    seen[ids[j]] = true;
  }
  var location = resolveMailbox(Mail, payload.mailbox || "INBOX", payload.account || "");
  var items = [], errors = [], stopped = false, attempted = 0;
  for (var i = 0; i < ids.length; i += 1) {
    attempted += 1;
    try {
      items.push(readCommand(Mail, { message_id: ids[i], max_chars: payload.max_chars }, location));
    } catch (error) {
      var detail = asText(error.message || error);
      var code = detail.indexOf("-1743") !== -1 || detail.toLowerCase().indexOf("not authorized") !== -1 ? "mail_permission_denied" :
        detail.indexOf("-1712") !== -1 || detail.toLowerCase().indexOf("timed out") !== -1 ? "mail_automation_timeout" : "message_read_failed";
      errors.push({ id: ids[i], code: code });
      if (code !== "message_read_failed") { stopped = true; break; }
    }
  }
  return { items: items, errors: errors, stopped_early: stopped, not_attempted_ids: ids.slice(attempted) };
}

function draftById(Mail, id) {
  var draft = Mail.outgoingMessages.byId(Number(id));
  if (Number(draft.id()) !== Number(id)) throw new Error("Open outgoing draft not found: " + id);
  return draft;
}

function draftSnapshot(draft) {
  var attachments = null;
  try {
    attachments = draft.content.attachments().map(function (attachment) {
      return { path: asText(attachment.fileName()) };
    });
  } catch (ignored) { /* Unknown must remain distinguishable from no attachments. */ }
  function addresses(property) {
    return draft[property]().map(function (recipient) { return asText(recipient.address()); });
  }
  return { draft_id: Number(draft.id()), sender: asText(draft.sender()), subject: asText(draft.subject()),
    content: asText(draft.content()), to: addresses("toRecipients"), cc: addresses("ccRecipients"),
    bcc: addresses("bccRecipients"), attachments: attachments, visible: Boolean(draft.visible()) };
}

function guardDraft(draft, expected) {
  var current = draftSnapshot(draft);
  if (!expected || Object.keys(current).some(function (key) { return JSON.stringify(current[key]) !== JSON.stringify(expected[key]); })) {
    throw new Error("Draft changed since verification. Read and verify it again.");
  }
}

function verifySendDraft(draft, payload) {
  var current = draftSnapshot(draft);
  function address(value) {
    var text = lower(value).trim();
    var match = text.match(/<([^<>]+)>$/);
    return match ? match[1].trim() : text;
  }
  function sameAddresses(actual, expected) {
    return JSON.stringify(actual.map(address).sort()) === JSON.stringify(expected.map(address).sort());
  }
  if (address(current.sender) !== payload.sender || current.subject !== payload.subject || current.content !== payload.body ||
      !sameAddresses(current.to, payload.to) || !sameAddresses(current.cc, payload.cc) || !sameAddresses(current.bcc, payload.bcc)) {
    throw new Error("Created draft differs from the approved intent; it remains unsent.");
  }
  if (!Array.isArray(current.attachments)) throw new Error("Draft attachments are unreadable; it remains unsent.");
  var paths = current.attachments.map(function (item) {
    var value = item.path;
    if (value.indexOf("file://localhost/") === 0) value = decodeURIComponent(value.slice(16));
    else if (value.indexOf("file:///") === 0) value = decodeURIComponent(value.slice(7));
    if (value.charAt(0) !== "/") throw new Error("Draft attachment reference is not absolute.");
    return value;
  }).sort();
  if (JSON.stringify(paths) !== JSON.stringify(payload.attachments.slice().sort())) {
    throw new Error("Draft attachment references differ from approved private snapshots.");
  }
  // Recheck the snapshot bytes immediately before dispatch, without issuing a
  // second one-use claim. Names/sizes/hashes were bound by validateAndClaimSend.
  payload.attachments.forEach(function (name, index) {
    var data = $.NSData.dataWithContentsOfFile(name);
    if (!data || Number(data.length) !== payload.attachment_evidence[index].size_bytes ||
        sha256Data(data) !== payload.attachment_evidence[index].sha256) throw new Error("Attachment snapshot changed before sending.");
  });
  guardDraft(draft, current);
  return current;
}

function addRecipients(Mail, outgoing, kind, addresses) {
  var values = addresses || [];
  for (var i = 0; i < values.length; i += 1) {
    var recipient;
    if (kind === "to") recipient = Mail.ToRecipient({ address: values[i] });
    if (kind === "cc") recipient = Mail.CcRecipient({ address: values[i] });
    if (kind === "bcc") recipient = Mail.BccRecipient({ address: values[i] });
    outgoing[kind + "Recipients"].push(recipient);
  }
}

function addAttachments(Mail, outgoing, paths) {
  var values = paths || [];
  for (var i = 0; i < values.length; i += 1) {
    outgoing.content.attachments.push(Mail.Attachment({ fileName: Path(values[i]) }));
  }
}

function validateSender(Mail, sender) {
  if (sender) {
    var wanted = lower(sender).trim();
    var accounts = getAccounts(Mail);
    var matches = 0;
    for (var i = 0; i < accounts.length; i += 1) {
      if (!callOr(accounts[i], "enabled", false)) continue;
      var addresses = callOr(accounts[i], "emailAddresses", []);
      for (var j = 0; j < addresses.length; j += 1) {
        if (lower(addresses[j]).trim() === wanted) { matches += 1; break; }
      }
    }
    if (matches === 0) throw new Error("Sender is not configured on an enabled Mail account.");
    if (matches > 1) throw new Error("Sender belongs to multiple enabled Mail accounts; resolve the ambiguity before continuing.");
  }
}

function createOutgoing(Mail, payload, visible) {
  if (!payload.to || payload.to.length === 0) throw new Error("At least one recipient is required.");
  validateSender(Mail, payload.sender);
  var outgoing = Mail.OutgoingMessage({ subject: asText(payload.subject), content: asText(payload.body), visible: Boolean(visible) });
  Mail.outgoingMessages.push(outgoing);
  if (payload.sender) outgoing.sender = payload.sender;
  addRecipients(Mail, outgoing, "to", payload.to);
  addRecipients(Mail, outgoing, "cc", payload.cc || []);
  addRecipients(Mail, outgoing, "bcc", payload.bcc || []);
  addAttachments(Mail, outgoing, payload.attachments || []);
  return outgoing;
}

function canonicalJson(value) {
  if (Array.isArray(value)) return "[" + value.map(canonicalJson).join(",") + "]";
  if (value && typeof value === "object") {
    return "{" + Object.keys(value).sort().map(function (key) {
      return JSON.stringify(key) + ":" + canonicalJson(value[key]);
    }).join(",") + "}";
  }
  return JSON.stringify(value);
}

function sha256Data(data) {
  // Use macOS CommonCrypto, not a custom cryptographic implementation.
  ObjC.bindFunction("CC_SHA256", ["void *", ["void *", "unsigned int", "void *"]]);
  var digest = $.NSMutableData.dataWithLength(32);
  $.CC_SHA256(data.bytes, data.length, digest.mutableBytes);
  var encoded = ObjC.unwrap(digest.base64EncodedStringWithOptions(0));
  var alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  var bits = 0, count = 0, result = "";
  for (var i = 0; i < encoded.length; i += 1) {
    if (encoded[i] === "=") break;
    bits = (bits << 6) | alphabet.indexOf(encoded[i]);
    count += 6;
    if (count >= 8) {
      count -= 8;
      var byte = (bits >>> count) & 255;
      result += ("0" + byte.toString(16)).slice(-2);
    }
  }
  return result;
}

function sha256Text(text) {
  var data = $(text).dataUsingEncoding($.NSUTF8StringEncoding);
  if (!data) throw new Error("Outbound intent is not valid UTF-8.");
  return sha256Data(data);
}

function sendIntent(payload) {
  if (typeof payload.sender !== "string" || !payload.sender || typeof payload.subject !== "string" || !payload.subject || typeof payload.body !== "string" || !payload.body) throw new Error("Incomplete outbound intent.");
  ["to", "cc", "bcc"].forEach(function (key) {
    if (!Array.isArray(payload[key]) || payload[key].some(function (address) { return typeof address !== "string" || !address; })) throw new Error("Invalid outbound recipients.");
  });
  if (!payload.to.length) throw new Error("At least one To recipient is required.");
  var evidence = payload.attachment_evidence;
  if (!Array.isArray(evidence) || evidence.length > 20 || !Array.isArray(payload.attachments) || payload.attachments.length !== evidence.length) throw new Error("Invalid attachment evidence.");
  var total = 0;
  var descriptors = evidence.map(function (item) {
    if (!item || typeof item.name !== "string" || !item.name || item.name === "." || item.name === ".." || /[\/\u0000]/.test(item.name) || typeof item.size_bytes !== "number" || item.size_bytes < 0 || item.size_bytes !== Math.floor(item.size_bytes) || typeof item.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(item.sha256)) throw new Error("Invalid attachment descriptor.");
    total += item.size_bytes;
    return { name: item.name, size_bytes: item.size_bytes, sha256: item.sha256 };
  });
  if (total > 25 * 1024 * 1024) throw new Error("Direct send attachment bytes exceed 25 MiB.");
  return { schema: "mailctl-send-intent/v1", sender: payload.sender, to: payload.to,
    cc: payload.cc, bcc: payload.bcc, subject: payload.subject, body: payload.body,
    attachments: descriptors };
}

function privateItem(manager, path, type, mode, uid) {
  var attributes = ObjC.deepUnwrap(manager.attributesOfItemAtPathError(path, null));
  if (!attributes || attributes.NSFileType !== type || Number(attributes.NSFileOwnerAccountID) !== uid || (Number(attributes.NSFilePosixPermissions) & 511) !== mode) throw new Error("Unsafe or missing private send state.");
  return attributes;
}

function sendStateRoot() {
  return ObjC.unwrap($.NSHomeDirectory()) + "/Library/Application Support/apple-mail-cli/send-attempts/v1";
}

function validateAndClaimSend(payload) {
  if (typeof payload.confirm_send !== "string" || !/^[0-9a-f]{64}$/.test(payload.confirm_send)) throw new Error("Direct send requires the exact approved intent digest.");
  var intent = sendIntent(payload);
  var digest = sha256Text(canonicalJson(intent));
  if (digest !== payload.confirm_send) throw new Error("Outbound intent changed after confirmation.");
  ObjC.bindFunction("getuid", ["unsigned int", []]);
  var uid = Number($.getuid());
  var manager = $.NSFileManager.defaultManager;
  var root = sendStateRoot();
  var directory = root + "/" + digest;
  privateItem(manager, root, "NSFileTypeDirectory", 448, uid);
  privateItem(manager, directory, "NSFileTypeDirectory", 448, uid);
  privateItem(manager, directory + "/attempt.json", "NSFileTypeRegular", 384, uid);
  var recordData = $.NSData.dataWithContentsOfFile(directory + "/attempt.json");
  var record = JSON.parse(ObjC.unwrap($.NSString.alloc.initWithDataEncoding(recordData, $.NSUTF8StringEncoding)));
  if (record.schema !== "mailctl-send-attempt/v1" || record.digest !== digest || record.state !== "in_flight") throw new Error("Send intent lacks its in-flight local reservation.");
  for (var i = 0; i < intent.attachments.length; i += 1) {
    var item = intent.attachments[i];
    var path = directory + "/attachments/" + i + "/" + item.name;
    if (payload.attachments[i] !== path) throw new Error("Attachment is outside the approved private snapshot.");
    privateItem(manager, directory + "/attachments", "NSFileTypeDirectory", 448, uid);
    privateItem(manager, directory + "/attachments/" + i, "NSFileTypeDirectory", 448, uid);
    var attributes = privateItem(manager, path, "NSFileTypeRegular", 256, uid);
    if (Number(attributes.NSFileSize) !== item.size_bytes) throw new Error("Approved attachment byte size changed.");
    var bytes = $.NSData.dataWithContentsOfFile(path);
    if (Number(bytes.length) !== item.size_bytes || sha256Data(bytes) !== item.sha256) throw new Error("Approved attachment bytes changed.");
  }
  // Atomic mkdir: even a replay of the same raw backend request cannot claim
  // this local reservation a second time. Never remove this marker on error.
  if (!manager.createDirectoryAtPathWithIntermediateDirectoriesAttributesError(directory + "/backend-claimed", false, $({ NSFilePosixPermissions: 448 }), null)) throw new Error("This send attempt was already claimed or cannot be claimed.");
  ObjC.bindFunction("open", ["int", ["char *", "int", "int"]]);
  ObjC.bindFunction("fsync", ["int", ["int"]]);
  ObjC.bindFunction("close", ["int", ["int"]]);
  var descriptor = Number($.open(directory, 0, 0));
  if (descriptor < 0) throw new Error("Cannot persist the one-use send claim.");
  try {
    if (Number($.fsync(descriptor)) !== 0) throw new Error("Cannot persist the one-use send claim.");
  } finally { $.close(descriptor); }
  return digest;
}

function run(argv) {
  var command = "unknown";
  try {
    // mailctl passes the request over UTF-8 stdin, never process arguments.
    ObjC.import("Foundation");
    var input = $.NSFileHandle.fileHandleWithStandardInput.readDataToEndOfFile;
    var text = ObjC.unwrap($.NSString.alloc.initWithDataEncoding(input, $.NSUTF8StringEncoding));
    var payload = JSON.parse(text);
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("Invalid backend request.");
    command = asText(payload.command);
    if (command === "ping") {
      return JSON.stringify({ ok: true, command: command, data: { backend: "apple-mail-jxa", version: "0.6.0" } });
    }

    var supported = ["accounts", "mailboxes", "list", "read", "read_batch", "selection", "probe", "resolve", "draft_read", "draft_update", "attachments_list", "attachment_save", "mark", "flag", "move", "trash", "compose", "send", "reply"];
    if (supported.indexOf(command) === -1) throw new Error("Unknown backend command: " + command);
    if (["mark", "flag", "move", "draft_update"].indexOf(command) !== -1 && payload.confirm_change !== true) throw new Error("Mailbox change requires --confirm-change.");
    if (command === "trash" && payload.confirm_trash !== true) throw new Error("Moving mail to Trash requires --confirm-trash.");
    var sendDigest = command === "send" ? validateAndClaimSend(payload) : null;

    var Mail = Application("Mail");
    var data;
    if (command === "probe") data = { automation_readable: true, account_count: Mail.accounts().length };
    else if (command === "draft_read") data = draftSnapshot(draftById(Mail, payload.draft_id));
    else if (command === "draft_update") {
      if (!payload.confirm_change) throw new Error("Draft update requires --confirm-change.");
      var editing = draftById(Mail, payload.draft_id);
      guardDraft(editing, payload.expected_snapshot);
      validateSender(Mail, payload.sender || payload.expected_snapshot.sender);
      editing.subject = payload.subject;
      editing.sender = payload.sender || payload.expected_snapshot.sender;
      editing.content = payload.body;
      ["to", "cc", "bcc"].forEach(function (kind) {
        editing[kind + "Recipients"]().forEach(function (recipient) { Mail.delete(recipient); });
        addRecipients(Mail, editing, kind, payload[kind] || []);
      });
      addAttachments(Mail, editing, payload.attachments || []);
      data = draftSnapshot(editing);
    } else if (command === "resolve") {
      var resolveLocation = resolveMailbox(Mail, payload.mailbox || "INBOX", payload.account || "");
      var resolved = resolveLocation.box.messages.whose({ messageId: payload.rfc_message_id })();
      resolved = resolved.filter(function (item) { return accountMatches(item, resolveLocation.accountFilter); });
      if (resolved.length !== 1) throw new Error("Message-ID did not resolve to exactly one message in this mailbox.");
      data = messageSummary(resolved[0], resolveLocation.path);
    } else if (command === "attachments_list") {
      data = attachmentList(messageById(resolveMailbox(Mail, payload.mailbox || "INBOX", payload.account || ""), payload.message_id));
    } else if (command === "attachment_save") {
      var attachmentLocation = resolveMailbox(Mail, payload.mailbox || "INBOX", payload.account || "");
      var attachmentMessage = messageById(attachmentLocation, payload.message_id);
      var attachmentMatches = attachmentMessage.mailAttachments().filter(function (item) { return asText(item.id()) === payload.attachment_id; });
      if (attachmentMatches.length !== 1) throw new Error("Attachment ID did not match exactly one attachment.");
      Mail.save(attachmentMatches[0], { in: Path(payload.output) });
      data = { saved_to: payload.output, attachment_id: payload.attachment_id };
    } else if (command === "accounts") data = accountsCommand(Mail);
    else if (command === "mailboxes") data = mailboxesCommand(Mail, payload);
    else if (command === "list") data = listCommand(Mail, payload);
    else if (command === "read") data = readCommand(Mail, payload);
    else if (command === "read_batch") data = readBatchCommand(Mail, payload);
    else if (command === "selection") {
      var selected = Mail.selection();
      data = [];
      for (var i = 0; i < selected.length; i += 1) data.push(messageSummary(selected[i], "selection"));
    } else if (command === "mark") {
      if (!payload.confirm_change) throw new Error("Mailbox change requires --confirm-change.");
      var markLocation = resolveMailbox(Mail, payload.mailbox || "INBOX", payload.account || "");
      var markMessage = messageById(markLocation, payload.message_id);
      markMessage.readStatus = Boolean(payload.read);
      data = { id: Number(payload.message_id), read: Boolean(markMessage.readStatus()), verified: Boolean(markMessage.readStatus()) === Boolean(payload.read) };
    } else if (command === "flag") {
      if (!payload.confirm_change) throw new Error("Mailbox change requires --confirm-change.");
      var flagLocation = resolveMailbox(Mail, payload.mailbox || "INBOX", payload.account || "");
      var flagMessage = messageById(flagLocation, payload.message_id);
      flagMessage.flaggedStatus = Boolean(payload.flagged);
      data = { id: Number(payload.message_id), flagged: Boolean(flagMessage.flaggedStatus()), verified: Boolean(flagMessage.flaggedStatus()) === Boolean(payload.flagged) };
    } else if (command === "move") {
      if (!payload.confirm_change) throw new Error("Mailbox move requires --confirm-change.");
      var fromLocation = resolveMailbox(Mail, payload.mailbox || "INBOX", payload.account || "");
      var targetLocation = resolveMailbox(Mail, payload.to_mailbox, payload.to_account || "");
      Mail.move(messageById(fromLocation, payload.message_id), { to: targetLocation.box });
      data = { id: Number(payload.message_id), requested_destination: targetLocation.path, account: targetLocation.accountFilter || null, accepted: true, verified: false };
    } else if (command === "trash") {
      if (!payload.confirm_trash) throw new Error("Moving mail to Trash requires --confirm-trash.");
      var trashLocation = resolveMailbox(Mail, payload.mailbox || "INBOX", payload.account || "");
      Mail.delete(messageById(trashLocation, payload.message_id));
      data = { id: Number(payload.message_id), trash_requested: true, accepted: true, verified: false };
    } else if (command === "compose") {
      var draft = createOutgoing(Mail, payload, true);
      data = draftSnapshot(draft);
      data.sent = false;
    } else if (command === "send") {
      var outgoing = createOutgoing(Mail, payload, true);
      verifySendDraft(outgoing, payload);
      var sent = Boolean(outgoing.send());
      if (!sent) throw new Error("Mail did not accept the outgoing message for sending.");
      data = { submission_accepted: true, delivery_confirmed: false, outcome: "accepted_by_mail", intent_digest: sendDigest };
    } else if (command === "reply") {
      var replyLocation = resolveMailbox(Mail, payload.mailbox || "INBOX", payload.account || "");
      var original = messageById(replyLocation, payload.message_id);
      var reply = Mail.reply(original, { openingWindow: true, replyToAll: Boolean(payload.reply_all) });
      reply.content = asText(payload.body) + "\n\n" + asText(callOr(reply, "content", ""));
      reply.visible = true;
      data = draftSnapshot(reply);
      data.original_id = Number(payload.message_id);
      data.sent = false;
    } else {
      throw new Error("Unknown backend command: " + command);
    }
    return JSON.stringify({ ok: true, command: command, data: data });
  } catch (error) {
    return JSON.stringify({ ok: false, command: command, error: { type: asText(error.name || "Error"), message: asText(error.message || error) } });
  }
}
