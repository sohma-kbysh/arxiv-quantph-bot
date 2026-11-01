/**
 * Optional Google Apps Script bridge for LanguageApp.translate.
 * Set API_TOKEN in Script Properties; never put the token in this source.
 * Deploy as a Web app, executing as yourself, with access set to Anyone.
 * See docs/FREE_TRANSLATION.md. No Cloud Translation API key is required.
 */
function doPost(e) {
  var expectedToken = PropertiesService.getScriptProperties().getProperty('API_TOKEN');
  if (!expectedToken || expectedToken.length < 32 || expectedToken.length > 256) {
    return jsonResponse_({error: 'service_not_configured', translations: []});
  }

  var raw = e && e.postData && e.postData.contents;
  // Limit parsing work as well as the translated character count below.
  if (typeof raw !== 'string' || raw.length > 24000) {
    return jsonResponse_({error: 'invalid_request', translations: []});
  }

  var request;
  try {
    request = JSON.parse(raw);
  } catch (ignored) {
    return jsonResponse_({error: 'invalid_json', translations: []});
  }
  if (!request || typeof request !== 'object' || Array.isArray(request)) {
    return jsonResponse_({error: 'invalid_request', translations: []});
  }
  if (!tokensEqual_(expectedToken, request.token)) {
    return jsonResponse_({error: 'unauthorized', translations: []});
  }

  var texts = request.texts;
  var source = request.source_language === undefined ? 'en' : request.source_language;
  var target = request.target_language === undefined ? 'ja' : request.target_language;
  var languagePattern = /^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$/;
  if (!Array.isArray(texts) || texts.length < 1 || texts.length > 5 ||
      typeof source !== 'string' || typeof target !== 'string' ||
      !languagePattern.test(source) || !languagePattern.test(target)) {
    return jsonResponse_({error: 'invalid_request', translations: []});
  }

  var totalCharacters = 0;
  for (var i = 0; i < texts.length; i++) {
    if (typeof texts[i] !== 'string' || texts[i].trim().length === 0) {
      return jsonResponse_({error: 'invalid_request', translations: []});
    }
    totalCharacters += texts[i].length;
  }
  if (totalCharacters > 20000) {
    return jsonResponse_({error: 'text_limit_exceeded', translations: []});
  }

  try {
    var translations = texts.map(function (text) {
      var translated = LanguageApp.translate(text, source, target);
      if (typeof translated !== 'string' || translated.trim().length === 0) {
        throw new Error('empty_translation');
      }
      return translated;
    });
    return jsonResponse_({translations: translations});
  } catch (ignored) {
    // Do not echo requests, tokens, or exception messages to the caller.
    // A failed batch can have consumed quota for its earlier texts.
    return jsonResponse_({error: 'translation_failed', translations: []});
  }
}

function tokensEqual_(expected, supplied) {
  if (typeof supplied !== 'string' || supplied.length > 256) {
    return false;
  }
  var difference = expected.length ^ supplied.length;
  // Avoid stopping at the first mismatched character.
  for (var i = 0; i < 256; i++) {
    difference |= (expected.charCodeAt(i) || 0) ^ (supplied.charCodeAt(i) || 0);
  }
  return difference === 0;
}

function jsonResponse_(body) {
  return ContentService.createTextOutput(JSON.stringify(body))
    .setMimeType(ContentService.MimeType.JSON);
}
