#!/usr/bin/env python3
"""Local speech-to-text: SenseVoiceSmall behind an OpenAI-compatible API.

  this file  -- the transcriber: POST /v1/audio/transcriptions
  tea_agent  -- the channel:  POST /api/asr/transcribe (forwards here)

The agent knows nothing about the model: it is told where the transcription
service lives (the `asr:` section of provider.yaml) and forwards the recorded
clip there. That is the same split as the fleet: the heavy thing runs where the
heavy thing belongs, and the agent stays a single small binary that a device
can run.

Why SenseVoiceSmall: it is a small (234M) multilingual model - Chinese,
Yue, English, Japanese, Korean - with good Chinese accuracy, and the ONNX
build runs on CPU without a GPU. The int8 model is ~240 MB and transcribes a
sentence in well under a second on an ordinary machine.

Usage:

  # one-time: fetch the model (~240 MB) into ~/.tea_agent/asr
  python3 sched/asr_server.py --download

  # serve on 127.0.0.1:8284
  python3 sched/asr_server.py

  # what does it hear?
  curl -F file=@hello.wav http://127.0.0.1:8284/v1/audio/transcriptions

  # is it up?
  curl http://127.0.0.1:8284/health

The model runtime is the one dependency here: `pip install sherpa-onnx`.
Everything else is the standard library, so the service starts on a device
that has nothing but Python. The other files in this directory are stdlib
only; this one cannot be, because it does actual neural inference.
"""

import argparse
import array
import json
import os
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import wave

try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except ImportError:  # Python 3.6
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        """The 3.6 spelling of http.server.ThreadingHTTPServer."""
        daemon_threads = True


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8284
DEFAULT_MODEL_DIR = os.path.join(os.path.expanduser("~"), ".tea_agent", "asr")

# Where the SenseVoiceSmall ONNX build lives. The ModelScope copy is listed
# first: it is the official Alibaba mirror and it is reachable from networks
# where huggingface.co is not (which is most of the places this runs).
MODEL_SOURCES = [
    {
        "name": "modelscope",
        "repo": "fengge2024/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
        "files": {
            "model.int8.onnx": "model.int8.onnx",
            "tokens.txt": "tokens.txt",
        },
        "url": "https://www.modelscope.cn/api/v1/models/{repo}/repo"
               "?Revision=master&FilePath={path}",
    },
    {
        "name": "huggingface",
        "repo": "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-onnx",
        "files": {
            "model.int8.onnx": "model.int8.onnx",
            "tokens.txt": "tokens.txt",
        },
        "url": "https://huggingface.co/{repo}/resolve/main/{path}",
    },
]

SAMPLE_RATE = 16000
MAX_BODY = 32 * 1024 * 1024

# The recognised text can carry event tags ("<|HAPPY|>", "<|zh|>"). They are
# useful for a caller that wants the emotion, and noise for one that wants to
# read the sentence, so they are stripped from the answer.
_TAG_CHARS = set("<|>[]{}")


def log(message):
    sys.stderr.write("[asr] %s\n" % message)
    sys.stderr.flush()


# --------------------------------------------------------------------------
# the model
# --------------------------------------------------------------------------

class Recognizer(object):
    """The SenseVoiceSmall recognizer, loaded once and shared.

    sherpa-onnx's stream objects are not safe to use from several threads at
    once, so the decode is serialised. One sentence at a time is plenty: the
    model answers in a fraction of a second.
    """

    def __init__(self, model_path, tokens_path, num_threads=2, use_itn=True):
        import sherpa_onnx  # imported late: the download must work without it

        self._lock = threading.Lock()
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=model_path,
            tokens=tokens_path,
            num_threads=num_threads,
            use_itn=use_itn,
        )

    def transcribe(self, samples, sample_rate):
        """`samples` is a list of floats in [-1, 1]."""
        with self._lock:
            stream = self._recognizer.create_stream()
            stream.accept_waveform(sample_rate, samples)
            self._recognizer.decode_stream(stream)
            return stream.result.text or ""


def strip_tags(text):
    """Drop the event tags SenseVoice attaches to the text."""
    out = []
    depth = 0
    for ch in text:
        if ch in _TAG_CHARS:
            depth += 1
            continue
        if depth:
            continue
        out.append(ch)
    return "".join(out).strip()


# --------------------------------------------------------------------------
# audio
# --------------------------------------------------------------------------

def decode_wav(data):
    """WAV bytes -> (samples as floats, sample rate).

    Returns None when the bytes are not a WAV file this can read.
    """
    import io

    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError, ValueError):
        return None

    if width == 1:
        raw = array.array("B")
        raw.frombytes(frames)
        samples = [(b - 128) / 128.0 for b in raw]
    elif width == 2:
        raw = array.array("h")
        raw.frombytes(frames)
        samples = [b / 32768.0 for b in raw]
    elif width == 4:
        raw = array.array("i")
        raw.frombytes(frames)
        samples = [b / 2147483648.0 for b in raw]
    else:
        return None

    if channels > 1:
        samples = [sum(samples[i:i + channels]) / float(channels)
                   for i in range(0, len(samples) - channels + 1, channels)]

    if rate != SAMPLE_RATE and len(samples) > 1:
        samples = resample(samples, rate, SAMPLE_RATE)
        rate = SAMPLE_RATE
    return samples, rate


def resample(samples, src_rate, dst_rate):
    """Linear resampling. Good enough for speech at 16 kHz."""
    if src_rate == dst_rate or not samples:
        return samples
    ratio = float(dst_rate) / float(src_rate)
    count = int(len(samples) / ratio)
    out = []
    last = len(samples) - 1
    for i in range(count):
        pos = i * ratio
        left = int(pos)
        if left >= last:
            out.append(samples[last])
            break
        frac = pos - left
        out.append(samples[left] * (1.0 - frac) + samples[left + 1] * frac)
    return out


# --------------------------------------------------------------------------
# multipart
# --------------------------------------------------------------------------

def parse_multipart(body, content_type):
    """Pull the `file` part out of a multipart/form-data body.

    Returns the bytes of the first part named `file` (or the first part at
    all, which is what a hand-written curl -F file=@x.wav sends), or None.
    """
    boundary = None
    for chunk in content_type.split(";"):
        chunk = chunk.strip()
        if chunk.lower().startswith("boundary="):
            boundary = chunk[len("boundary="):].strip()
            if boundary.startswith('"') and boundary.endswith('"'):
                boundary = boundary[1:-1]
    if not boundary:
        return None

    marker = ("--" + boundary).encode("ascii")
    # The body is binary, so work on bytes and split on the byte boundary.
    parts = body.split(marker)
    for part in parts[1:]:
        if part[:2] == b"--":
            break
        part = part.lstrip(b"\r\n")
        split = part.find(b"\r\n\r\n")
        if split < 0:
            continue
        head = part[:split].decode("utf-8", "replace")
        data = part[split + 4:]
        if data.endswith(b"\r\n"):
            data = data[:-2]
        name = None
        for line in head.split("\r\n"):
            low = line.lower()
            if low.startswith("content-disposition:"):
                for piece in line.split(";"):
                    piece = piece.strip()
                    if piece.lower().startswith("name="):
                        name = piece[len("name="):].strip().strip('"')
        if name == "file" or (name is None and data):
            return data
    return None


# --------------------------------------------------------------------------
# the server
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "tea-asr/1.0"
    protocol_version = "HTTP/1.1"

    # Set by main().
    recognizer = None
    model_name = "sense-voice"

    def log_message(self, fmt, *args):
        log(fmt % args)

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] in ("/health", "/v1/health"):
            self._send_json(200, {
                "status": "ok" if self.recognizer else "no-model",
                "model": self.model_name,
                "sample_rate": SAMPLE_RATE,
            })
            return
        if self.path.split("?")[0] == "/v1/models":
            self._send_json(200, {
                "object": "list",
                "data": [{"id": self.model_name, "object": "model", "owned_by": "local"}],
            })
            return
        self._send_json(404, {"error": {"message": "no such endpoint: " + self.path}})

    def do_POST(self):
        if self.path.split("?")[0] != "/v1/audio/transcriptions":
            self._send_json(404, {"error": {"message": "no such endpoint: " + self.path}})
            return
        if not self.recognizer:
            self._send_json(503, {"error": {"message": "the model is not loaded"}})
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send_json(400, {"error": {"message": "the request body is empty"}})
            return
        if length > MAX_BODY:
            self._send_json(413, {"error": {"message": "the audio clip is too large"}})
            return
        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type") or ""

        audio = None
        if content_type.lower().startswith("multipart/form-data"):
            audio = parse_multipart(body, content_type)
        else:
            audio = body
        if not audio:
            self._send_json(400, {
                "error": {"message": "no audio in the request; send it as multipart/form-data "
                                      "with a 'file' part, or as the raw body"}
            })
            return

        decoded = decode_wav(audio)
        if decoded is None:
            self._send_json(400, {"error": {"message": "the audio is not a readable WAV file"}})
            return
        samples, rate = decoded
        if not samples:
            self._send_json(400, {"error": {"message": "the audio clip is empty"}})
            return

        started = time.time()
        try:
            text = self.recognizer.transcribe(samples, rate)
        except Exception as exc:  # a broken model must not kill the service
            log("recognition failed: %s" % exc)
            self._send_json(500, {"error": {"message": "recognition failed: %s" % exc}})
            return
        elapsed = time.time() - started
        log("transcribed %d samples in %.2fs" % (len(samples), elapsed))
        self._send_json(200, {"text": strip_tags(text)})


# --------------------------------------------------------------------------
# the model download
# --------------------------------------------------------------------------

def download_file(source, remote_name, target, retries=3):
    url = source["url"].format(repo=source["repo"], path=source["files"][remote_name])
    last = None
    for attempt in range(retries):
        try:
            log("downloading %s ..." % remote_name)
            with urllib.request.urlopen(url, timeout=120) as response:
                data = response.read()
            if not data:
                raise IOError("empty response")
            with open(target, "wb") as out:
                out.write(data)
            log("  -> %s (%d bytes)" % (target, len(data)))
            return True
        except Exception as exc:
            last = exc
            log("  attempt %d/%d failed: %s" % (attempt + 1, retries, exc))
            time.sleep(2)
    log("could not fetch %s: %s" % (remote_name, last))
    return False


def download_model(model_dir, source_name=None):
    """Fetch the model and the tokens into `model_dir`."""
    if not os.path.isdir(model_dir):
        os.makedirs(model_dir)
    sources = MODEL_SOURCES
    if source_name:
        sources = [s for s in sources if s["name"] == source_name]
        if not sources:
            log("unknown source %r (known: %s)"
                % (source_name, ", ".join(s["name"] for s in MODEL_SOURCES)))
            return False
    for source in sources:
        log("trying %s (%s)" % (source["name"], source["repo"]))
        ok = True
        for remote_name, local_name in source["files"].items():
            target = os.path.join(model_dir, local_name)
            if os.path.exists(target) and os.path.getsize(target) > 0:
                log("  %s is already there" % local_name)
                continue
            if not download_file(source, remote_name, target):
                ok = False
                break
        if ok:
            log("model is ready in %s" % model_dir)
            return True
    log("no source worked; download the model by hand into %s" % model_dir)
    return False


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def find_model(model_dir):
    """The ONNX model and the tokens inside `model_dir`."""
    model = None
    tokens = None
    if os.path.isdir(model_dir):
        for name in sorted(os.listdir(model_dir)):
            path = os.path.join(model_dir, name)
            if not os.path.isfile(path):
                continue
            if name.endswith(".onnx") and model is None:
                model = path
            elif name == "tokens.txt" and tokens is None:
                tokens = path
    return model, tokens


def main(argv=None):
    parser = argparse.ArgumentParser(description="SenseVoiceSmall transcription service")
    parser.add_argument("--host", default=os.environ.get("TEA_ASR_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("TEA_ASR_PORT", DEFAULT_PORT)))
    parser.add_argument("--model-dir", default=os.environ.get("TEA_ASR_MODEL_DIR", DEFAULT_MODEL_DIR))
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--download", action="store_true",
                        help="fetch the model and exit")
    parser.add_argument("--source", default=None, help="modelscope | huggingface")
    args = parser.parse_args(argv)

    if args.download:
        return 0 if download_model(args.model_dir, args.source) else 1

    model, tokens = find_model(args.model_dir)
    if not model or not tokens:
        log("no model in %s" % args.model_dir)
        log("run:  python3 sched/asr_server.py --download")
        return 2

    try:
        log("loading %s ..." % os.path.basename(model))
        started = time.time()
        Handler.recognizer = Recognizer(model, tokens, num_threads=max(1, args.threads))
        log("model ready in %.1fs" % (time.time() - started))
    except ImportError:
        log("sherpa-onnx is not installed:  pip install sherpa-onnx")
        return 2
    except Exception as exc:
        log("cannot load the model: %s" % exc)
        return 2

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    log("listening on http://%s:%d  (POST /v1/audio/transcriptions)" % (args.host, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
