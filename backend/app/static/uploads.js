// Browser checks improve feedback. The server still inspects and decodes the audio.
export function validateAudioFile(file, info) {
  if (!info?.audio_upload_types)
    throw new Error("Audio settings are unavailable. Refresh the page and try again.");
  const dot = file.name.lastIndexOf(".");
  const suffix = dot > 0 ? file.name.slice(dot).toLowerCase() : "";
  const contentType = Object.hasOwn(info.audio_upload_types, suffix)
    ? info.audio_upload_types[suffix]
    : null;
  if (!contentType)
    throw new Error("Please choose a WAV, MP3, M4A, AAC or FLAC recording.");
  if (!file.size || file.size > info.max_upload_bytes)
    throw new Error("This file is empty or exceeds the upload limit.");
  return contentType;
}

export function audioUploadBody(file, info) {
  const contentType = validateAudioFile(file, info);
  const body = new FormData();
  // OS-provided MIME types vary or may be empty; use the API's canonical type.
  body.append("audio_file", file.slice(0, file.size, contentType), file.name);
  return body;
}
