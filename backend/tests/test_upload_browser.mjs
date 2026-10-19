import assert from "node:assert/strict";
import test from "node:test";

import { audioUploadBody, validateAudioFile } from "../app/static/uploads.js";

const info = {
  max_upload_bytes: 4,
  audio_upload_types: {
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".flac": "audio/flac",
  },
};

for (const [suffix, expectedType] of Object.entries(info.audio_upload_types)) {
  test(`${suffix} uploads retain bytes and filename and use the canonical multipart type`, async () => {
    const file = new File([new Uint8Array([1, 2, 3, 4])], `My recording${suffix.toUpperCase()}`, {
      type: "application/octet-stream",
    });
    assert.equal(validateAudioFile(file, info), expectedType);
    const body = audioUploadBody(file, info);
    assert.deepEqual([...body.keys()], ["audio_file"]);
    const uploaded = body.get("audio_file");
    assert.equal(uploaded.name, file.name);
    assert.equal(uploaded.type, expectedType);
    assert.deepEqual(new Uint8Array(await uploaded.arrayBuffer()), new Uint8Array([1, 2, 3, 4]));
  });
}

for (const name of ["recording.exe", "recording", ".mp3", "recording.mp3.exe"]) {
  test(`${name} is rejected even with an audio MIME type`, () => {
    assert.throws(() => validateAudioFile(new File(["data"], name, { type: "audio/mpeg" }), info),
      /choose a WAV, MP3, M4A, AAC or FLAC/);
  });
}

for (const data of ["", "12345"]) {
  test(`a ${data.length}-byte upload is rejected at a four-byte limit`, () => {
    const file = new File([data], "recording.mp3");
    assert.throws(() => validateAudioFile(file, info), /empty or exceeds/);
    assert.throws(() => audioUploadBody(file, info), /empty or exceeds/);
  });
}

test("submission revalidates the file if server limits have changed", () => {
  const file = new File(["data"], "recording.mp3");
  validateAudioFile(file, info);
  assert.throws(() => audioUploadBody(file, { ...info, max_upload_bytes: 3 }), /exceeds/);
});

test("file selection waits for server format settings", () => {
  assert.throws(() => validateAudioFile(new File(["data"], "recording.mp3"), null),
    /settings are unavailable/);
});
