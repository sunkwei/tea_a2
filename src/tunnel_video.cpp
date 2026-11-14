// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_video.cpp -- the pictures that travel through the tunnel.
//
// JPEG: GDI+ (the JPEG codec is in Windows; WIC's copy of it is not
// everywhere, so GDI+ is the one to use).
// H.264: Media Foundation (the encoder and the decoder MFT are in Windows).
//
// Everything else is a stub that says "not on this platform", so the
// service can answer with the truth instead of pretending.
#include "tunnel_video.h"

#include <cstdio>
#include <cstring>

#ifdef _WIN32
#include <windows.h>

#include <objidl.h>

#include <gdiplus/gdiplus.h>

#include <codecapi.h>
#include <mfapi.h>
#include <mferror.h>
#include <mfidl.h>
#include <mfobjects.h>
#include <mftransform.h>

// This toolchain's GDI+ headers put the flat API in a namespace.
using namespace Gdiplus;
using namespace Gdiplus::DllExports;
#endif

namespace tea_tunnel {

namespace {

inline uint8_t clamp_u8(int v) { return static_cast<uint8_t>(v < 0 ? 0 : (v > 255 ? 255 : v)); }

#ifdef _WIN32

/// Media Foundation wants one MFStartup for the process; the reference count
/// is what MFShutdown matches.
struct MfGuard {
  bool ok = false;
  MfGuard() { ok = SUCCEEDED(MFStartup(MF_VERSION, MFSTARTUP_LITE)); }
  ~MfGuard() {
    if (ok) MFShutdown();
  }
};

/// GDI+ wants one GdiplusStartup for the process.
struct GdiplusGuard {
  ULONG_PTR token = 0;
  bool ok = false;
  GdiplusGuard() {
    GdiplusStartupInput input;
    std::memset(&input, 0, sizeof(input));
    input.GdiplusVersion = 1;
    ok = GdiplusStartup(&token, &input, nullptr) == Gdiplus::Ok;
  }
  ~GdiplusGuard() {
    if (ok) GdiplusShutdown(token);
  }
};

/// MF_MT_FRAME_SIZE packs the width in the high half and the height in the
/// low half.
UINT64 frame_size(int width, int height) {
  return (static_cast<UINT64>(width) << 32) | static_cast<UINT64>(height);
}

void split_frame_size(UINT64 packed, int* width, int* height) {
  *width = static_cast<int>(packed >> 32);
  *height = static_cast<int>(packed & 0xffffffffULL);
}

void safe_release(IUnknown* p) {
  if (p) p->Release();
}

/// The frame size the codec should expect (0 = "whatever comes").
void set_frame_size(IMFMediaType* type, int width, int height) {
  if (width > 0 && height > 0) type->SetUINT32(MF_MT_FRAME_SIZE, frame_size(width, height));
}

/// A stream over a block of memory, so a codec can read and write bytes
/// without touching the disk.
///
/// The shape of IStream in this toolchain is the older one (Read and Write
/// take three parameters). That is fine: the codecs call these methods, and
/// an argument they pass that we do not use is simply ignored.
class MemoryStream : public IStream {
 public:
  explicit MemoryStream(std::string* bytes) : bytes_(bytes) {}

  // IUnknown
  HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void** ppv) override {
    if (!ppv) return E_POINTER;
    if (riid == IID_IUnknown || riid == IID_IStream) {
      *ppv = static_cast<IStream*>(this);
      AddRef();
      return S_OK;
    }
    *ppv = nullptr;
    return E_NOINTERFACE;
  }
  ULONG STDMETHODCALLTYPE AddRef() override {
    return static_cast<ULONG>(InterlockedIncrement(&refs_));
  }
  ULONG STDMETHODCALLTYPE Release() override {
    const LONG n = InterlockedDecrement(&refs_);
    if (n == 0) delete this;
    return static_cast<ULONG>(n);
  }

  // IStream
  HRESULT STDMETHODCALLTYPE Read(void* buffer, ULONG count, ULONG* read) override {
    if (read) *read = 0;
    if (!buffer) return E_POINTER;
    const std::size_t left = bytes_->size() - pos_;
    const ULONG take = static_cast<ULONG>(left < count ? left : count);
    if (take > 0) std::memcpy(buffer, bytes_->data() + pos_, take);
    pos_ += take;
    if (read) *read = take;
    return S_OK;
  }
  HRESULT STDMETHODCALLTYPE Write(const void* buffer, ULONG count, ULONG* written) override {
    if (!buffer) return E_POINTER;
    bytes_->append(static_cast<const char*>(buffer), count);
    pos_ = bytes_->size();
    if (written) *written = count;
    return S_OK;
  }
  HRESULT STDMETHODCALLTYPE Seek(LARGE_INTEGER move, DWORD origin, ULARGE_INTEGER* newpos) override {
    LONGLONG base = 0;
    if (origin == STREAM_SEEK_CUR) base = static_cast<LONGLONG>(pos_);
    else if (origin == STREAM_SEEK_END) base = static_cast<LONGLONG>(bytes_->size());
    LONGLONG next = base + move.QuadPart;
    if (next < 0) next = 0;
    pos_ = static_cast<std::size_t>(next);
    if (newpos) newpos->QuadPart = static_cast<ULONGLONG>(pos_);
    return S_OK;
  }
  HRESULT STDMETHODCALLTYPE SetSize(ULARGE_INTEGER size) override {
    bytes_->resize(static_cast<std::size_t>(size.QuadPart));
    return S_OK;
  }
  HRESULT STDMETHODCALLTYPE CopyTo(IStream* stream, ULARGE_INTEGER count, ULARGE_INTEGER* read,
                                   ULARGE_INTEGER* written) override {
    if (!stream) return E_POINTER;
    const std::size_t left = bytes_->size() - pos_;
    const ULONG take = static_cast<ULONG>(left < count.QuadPart ? left : count.QuadPart);
    if (take > 0) stream->Write(bytes_->data() + pos_, take, nullptr);
    pos_ += take;
    if (read) read->QuadPart = take;
    if (written) written->QuadPart = take;
    return S_OK;
  }
  HRESULT STDMETHODCALLTYPE Stat(STATSTG* stat, ULONG) override {
    if (!stat) return E_POINTER;
    stat->type = STGTY_STREAM;
    stat->cbSize.QuadPart = static_cast<ULONGLONG>(bytes_->size());
    stat->pwcsName = nullptr;
    return S_OK;
  }
  HRESULT STDMETHODCALLTYPE Clone(IStream** stream) override {
    if (!stream) return E_POINTER;
    *stream = nullptr;
    return E_NOTIMPL;
  }
  HRESULT STDMETHODCALLTYPE Commit(DWORD) override { return S_OK; }
  HRESULT STDMETHODCALLTYPE Revert() override { return S_OK; }
  HRESULT STDMETHODCALLTYPE LockRegion(ULARGE_INTEGER, ULARGE_INTEGER, DWORD) override { return S_OK; }
  HRESULT STDMETHODCALLTYPE UnlockRegion(ULARGE_INTEGER, ULARGE_INTEGER, DWORD) override { return S_OK; }

 private:
  std::string* bytes_;
  std::size_t pos_ = 0;
  LONG refs_ = 1;
};

/// The encoder GDI+ uses for JPEG. It is looked up by its MIME type: a
/// hard-coded CLSID is a guess, and the wrong guess writes PNG (a picture
/// ten times too big for the tunnel).
const CLSID& jpeg_encoder_clsid() {
  static CLSID clsid = {};
  static bool tried = false;
  if (!tried) {
    tried = true;
    UINT count = 0;
    UINT size = 0;
    if (GetImageEncodersSize(&count, &size) == Gdiplus::Ok && size > 0) {
      std::vector<BYTE> buffer(size);
      ImageCodecInfo* infos = reinterpret_cast<ImageCodecInfo*>(buffer.data());
      if (GetImageEncoders(count, size, infos) == Gdiplus::Ok) {
        for (UINT i = 0; i < count; i++) {
          if (infos[i].MimeType == nullptr) continue;
          if (std::wcscmp(infos[i].MimeType, L"image/jpeg") == 0) {
            clsid = infos[i].Clsid;
            break;
          }
        }
      }
    }
  }
  return clsid;
}

#endif  // _WIN32

}  // namespace

// ---------------------------------------------------------------------------
// JPEG
// ---------------------------------------------------------------------------
#ifdef _WIN32

bool jpeg_encode(const uint8_t* bgra, int width, int height, int stride, int quality,
                 std::string* out, std::string* err) {
  if (bgra == nullptr || width <= 0 || height <= 0) {
    if (err) *err = "there is no picture to encode";
    return false;
  }
  static GdiplusGuard gdiplus;
  if (!gdiplus.ok) {
    if (err) *err = "GDI+ did not start";
    return false;
  }

  GpBitmap* bitmap = nullptr;
  // The stride is the real one: GDI+ does not take zero here.
  GpStatus status = GdipCreateBitmapFromScan0(width, height, width * 4, PixelFormat32bppARGB,
                                              const_cast<BYTE*>(bgra), &bitmap);
  if (status != Gdiplus::Ok || bitmap == nullptr) {
    if (err) {
      char buf[120];
      std::snprintf(buf, sizeof(buf), "cannot make a picture out of the pixels (status=%d)",
                    static_cast<int>(status));
      *err = buf;
    }
    return false;
  }

  std::string bytes;
  MemoryStream stream(&bytes);
  // The quality: the lower it is, the smaller the picture (and the tunnel is
  // a narrow pipe).
  ULONG quality_value = quality > 0 ? static_cast<ULONG>(quality) : 75;
  EncoderParameter parameter;
  std::memset(&parameter, 0, sizeof(parameter));
  parameter.Guid = EncoderQuality;
  parameter.NumberOfValues = 1;
  parameter.Type = 4;   // EncoderParameterValueTypeLong
  parameter.Value = &quality_value;
  EncoderParameters parameters;
  std::memset(&parameters, 0, sizeof(parameters));
  parameters.Count = 1;
  parameters.Parameter[0] = parameter;
  status = GdipSaveImageToStream(bitmap, &stream, &jpeg_encoder_clsid(),
                                 quality > 0 ? &parameters : nullptr);
  GdipDisposeImage(reinterpret_cast<GpImage*>(bitmap));
  if (status != Gdiplus::Ok) {
    if (err) *err = "the JPEG encoder refused the picture";
    return false;
  }
  out->swap(bytes);
  return true;
}

bool jpeg_decode(const uint8_t* data, std::size_t len, std::vector<uint8_t>* bgra, int* width,
                 int* height, std::string* err) {
  if (data == nullptr || len == 0) {
    if (err) *err = "there is no picture to decode";
    return false;
  }
  static GdiplusGuard gdiplus;
  if (!gdiplus.ok) {
    if (err) *err = "GDI+ did not start";
    return false;
  }
  std::string bytes(reinterpret_cast<const char*>(data), len);
  MemoryStream stream(&bytes);

  GpBitmap* bitmap = nullptr;
  GpStatus status = GdipCreateBitmapFromStream(&stream, &bitmap);
  if (status != Gdiplus::Ok || bitmap == nullptr) {
    if (err) *err = "the JPEG picture could not be decoded";
    return false;
  }
  UINT w = 0;
  UINT h = 0;
  GdipGetImageWidth(reinterpret_cast<GpImage*>(bitmap), &w);
  GdipGetImageHeight(reinterpret_cast<GpImage*>(bitmap), &h);
  if (w == 0 || h == 0) {
    GdipDisposeImage(reinterpret_cast<GpImage*>(bitmap));
    if (err) *err = "the JPEG picture has no size";
    return false;
  }
  if (width) *width = static_cast<int>(w);
  if (height) *height = static_cast<int>(h);

  bool ok = true;
  if (bgra) {
    BitmapData locked;
    std::memset(&locked, 0, sizeof(locked));
    status = GdipBitmapLockBits(bitmap, nullptr, ImageLockModeRead, PixelFormat32bppARGB, &locked);
    if (status == Gdiplus::Ok && locked.Scan0 != nullptr) {
      bgra->assign(static_cast<std::size_t>(w) * h * 4, 0);
      const std::size_t row = static_cast<std::size_t>(w) * 4;
      for (UINT j = 0; j < h; j++) {
        std::memcpy(bgra->data() + static_cast<std::size_t>(j) * row,
                    static_cast<const BYTE*>(locked.Scan0) + static_cast<std::size_t>(j) * locked.Stride,
                    row);
      }
      GdipBitmapUnlockBits(bitmap, &locked);
    } else {
      ok = false;
    }
  }
  GdipDisposeImage(reinterpret_cast<GpImage*>(bitmap));
  if (!ok) {
    if (err) *err = "the JPEG picture could not be read";
    return false;
  }
  return true;
}

#else  // !_WIN32

bool jpeg_encode(const uint8_t* bgra, int width, int height, int stride, int quality,
                 std::string* out, std::string* err) {
  (void)bgra;
  (void)width;
  (void)height;
  (void)stride;
  (void)quality;
  (void)out;
  if (err) *err = "there is no JPEG encoder on this platform";
  return false;
}

bool jpeg_decode(const uint8_t* data, std::size_t len, std::vector<uint8_t>* bgra, int* width,
                 int* height, std::string* err) {
  (void)data;
  (void)len;
  (void)bgra;
  (void)width;
  (void)height;
  if (err) *err = "there is no JPEG decoder on this platform";
  return false;
}

#endif  // _WIN32

// ---------------------------------------------------------------------------
// The colours
// ---------------------------------------------------------------------------
void bgra_to_yuv(const uint8_t* bgra, int width, int height, int stride, YuvFrame* out) {
  if (bgra == nullptr || width <= 0 || height <= 0) return;
  out->width = width;
  out->height = height;
  const std::size_t y_size = static_cast<std::size_t>(width) * height;
  out->data.assign(y_size + y_size / 2, 0);
  uint8_t* y = out->data.data();
  uint8_t* u = y + y_size;
  uint8_t* v = u + y_size / 4;
  const int cw = (width + 1) / 2;
  for (int j = 0; j < height; j++) {
    const uint8_t* row = bgra + static_cast<std::size_t>(j) * stride;
    for (int i = 0; i < width; i++) {
      const int b = row[i * 4 + 0];
      const int g = row[i * 4 + 1];
      const int r = row[i * 4 + 2];
      y[static_cast<std::size_t>(j) * width + i] = clamp_u8((66 * r + 129 * g + 25 * b + 128) >> 8);
      if ((j % 2) == 0 && (i % 2) == 0) {
        const std::size_t at = static_cast<std::size_t>(j / 2) * cw + i / 2;
        u[at] = clamp_u8((-38 * r - 74 * g + 112 * b + 128) >> 8);
        v[at] = clamp_u8((112 * r - 94 * g - 18 * b + 128) >> 8);
      }
    }
  }
}

void yuv_to_bgra(const YuvFrame& in, BgraFrame* out) {
  if (in.width <= 0 || in.height <= 0 ||
      in.data.size() < static_cast<std::size_t>(in.width) * in.height * 3 / 2) {
    return;
  }
  const int width = in.width;
  const int height = in.height;
  const std::size_t y_size = static_cast<std::size_t>(width) * height;
  out->width = width;
  out->height = height;
  out->data.assign(static_cast<std::size_t>(width) * height * 4, 0);
  const uint8_t* y = in.data.data();
  const uint8_t* u = y + y_size;
  const uint8_t* v = u + y_size / 4;
  const int cw = (width + 1) / 2;
  uint8_t* dst = out->data.data();
  for (int j = 0; j < height; j++) {
    for (int i = 0; i < width; i++) {
      const std::size_t at = static_cast<std::size_t>(j / 2) * cw + i / 2;
      const int Y = y[static_cast<std::size_t>(j) * width + i] - 16;
      const int U = u[at] - 128;
      const int V = v[at] - 128;
      uint8_t* p = dst + (static_cast<std::size_t>(j) * width + i) * 4;
      p[0] = clamp_u8((298 * Y + 409 * V + 128) >> 8);
      p[1] = clamp_u8((298 * Y - 100 * U - 208 * V + 128) >> 8);
      p[2] = clamp_u8((298 * Y + 516 * U + 128) >> 8);
      p[3] = 255;
    }
  }
}

// ---------------------------------------------------------------------------
// The H.264 path (Media Foundation)
// ---------------------------------------------------------------------------
struct H264Encoder::Impl {
#ifdef _WIN32
  MfGuard mf;
  IMFTransform* transform = nullptr;
  int width = 0;
  int height = 0;
  int fps = 2;
#endif
};

H264Encoder::H264Encoder() : impl_(new Impl()) {}
H264Encoder::~H264Encoder() { stop(); }

#ifdef _WIN32

namespace {

/// Configure one candidate encoder for NV12 in and H.264 out.
bool configure_encoder(IMFTransform* transform, int width, int height, int bitrate,
                       std::string* err) {
  // The rate control lives on the transform: a constant bitrate is what the
  // tunnel wants - the point of the whole thing is a small, predictable
  // stream.
  IMFAttributes* attrs = nullptr;
  if (FAILED(transform->GetAttributes(&attrs)) || attrs == nullptr) {
    if (err) *err = "the encoder has no attributes";
    return false;
  }
  attrs->SetUINT32(CODECAPI_AVEncCommonRateControlMode,
                   static_cast<UINT32>(eAVEncCommonRateControlMode_CBR));
  attrs->SetUINT32(CODECAPI_AVEncCommonMeanBitRate, static_cast<UINT32>(bitrate));
  attrs->Release();

  IMFMediaType* in = nullptr;
  MFCreateMediaType(&in);
  in->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
  in->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12);
  set_frame_size(in, width, height);
  in->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
  in->SetUINT32(MF_MT_ALL_SAMPLES_INDEPENDENT, TRUE);
  in->SetUINT32(MF_MT_SAMPLE_SIZE, static_cast<UINT32>(width * height * 3 / 2));

  IMFMediaType* out = nullptr;
  MFCreateMediaType(&out);
  out->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
  out->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_H264);
  set_frame_size(out, width, height);
  out->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);

  // The order differs between the Windows versions, so try both.
  HRESULT hr = transform->SetInputType(0, in, 0);
  if (SUCCEEDED(hr)) hr = transform->SetOutputType(0, out, 0);
  if (FAILED(hr)) {
    hr = transform->SetOutputType(0, out, 0);
    if (SUCCEEDED(hr)) hr = transform->SetInputType(0, in, 0);
  }
  safe_release(in);
  safe_release(out);
  if (FAILED(hr)) {
    if (err) *err = "the encoder refused NV12 in / H.264 out";
    return false;
  }
  return true;
}

/// What came out of the transform.
enum class Pull { kSample, kNeedMore, kError };

/// Pull one output sample. The caller owns the sample when it gets one.
Pull pull_sample(IMFTransform* transform, IMFSample** sample) {
  *sample = nullptr;
  MFT_OUTPUT_DATA_BUFFER buffer;
  buffer.pSample = nullptr;
  buffer.dwStatus = 0;
  DWORD status = 0;
  const HRESULT hr = transform->ProcessOutput(0, 1, &buffer, &status);
  if (hr == MF_E_TRANSFORM_NEED_MORE_INPUT) return Pull::kNeedMore;
  if (FAILED(hr)) return Pull::kError;
  if (buffer.pSample == nullptr) return Pull::kNeedMore;
  *sample = buffer.pSample;
  return Pull::kSample;
}

/// The bytes of one output sample, in one piece.
bool sample_bytes(IMFSample* sample, std::string* out) {
  IMFMediaBuffer* media = nullptr;
  if (FAILED(sample->ConvertToContiguousBuffer(&media)) || media == nullptr) return false;
  BYTE* bytes = nullptr;
  DWORD len = 0;
  DWORD max_len = 0;
  bool ok = false;
  if (SUCCEEDED(media->Lock(&bytes, &max_len, &len)) && bytes != nullptr && len > 0) {
    out->append(reinterpret_cast<const char*>(bytes), len);
    media->Unlock();
    ok = true;
  }
  media->Release();
  return ok;
}

}  // namespace

bool H264Encoder::start(int width, int height, int fps, int bitrate, std::string* err) {
  if (running_) return true;
  if (!impl_->mf.ok) {
    if (err) *err = "Media Foundation did not start";
    return false;
  }
  if (width <= 0 || height <= 0 || (width % 2) != 0 || (height % 2) != 0) {
    if (err) *err = "the picture size must be even";
    return false;
  }

  MFT_REGISTER_TYPE_INFO in_type;
  in_type.guidMajorType = MFMediaType_Video;
  in_type.guidSubtype = MFVideoFormat_NV12;
  MFT_REGISTER_TYPE_INFO out_type;
  out_type.guidMajorType = MFMediaType_Video;
  out_type.guidSubtype = MFVideoFormat_H264;

  IMFActivate** activates = nullptr;
  UINT32 count = 0;
  // Every encoder, not just the hardware ones: the software encoder is
  // the one that is everywhere.
  HRESULT hr = MFTEnumEx(MFT_CATEGORY_VIDEO_ENCODER, 0, &in_type, &out_type, &activates,
                          &count);
  if (FAILED(hr) || activates == nullptr || count == 0) {
    if (err) *err = "this Windows has no H.264 encoder";
    return false;
  }

  std::string last_error = "no encoder accepted the picture size";
  for (UINT32 i = 0; i < count; i++) {
    IMFTransform* transform = nullptr;
    if (FAILED(activates[i]->ActivateObject(IID_IMFTransform, reinterpret_cast<void**>(&transform))) ||
        transform == nullptr) {
      continue;
    }
    std::string why;
    if (configure_encoder(transform, width, height, bitrate, &why)) {
      impl_->transform = transform;
      impl_->width = width;
      impl_->height = height;
      impl_->fps = fps > 0 ? fps : 2;
      running_ = true;
      CoTaskMemFree(activates);
      return true;
    }
    last_error = why;
    transform->Release();
  }
  CoTaskMemFree(activates);
  if (err) *err = last_error;
  return false;
}

bool H264Encoder::encode(const YuvFrame& frame, int64_t timestamp_ms, std::string* out,
                         std::string* err) {
  if (!running_ || impl_->transform == nullptr) {
    if (err) *err = "the encoder is not running";
    return false;
  }
  if (frame.width != impl_->width || frame.height != impl_->height ||
      frame.data.size() < static_cast<std::size_t>(frame.width) * frame.height * 3 / 2) {
    if (err) *err = "the picture does not match the encoder";
    return false;
  }

  IMFMediaBuffer* buffer = nullptr;
  const std::size_t size = frame.data.size();
  HRESULT hr = MFCreateMemoryBuffer(static_cast<DWORD>(size), &buffer);
  if (FAILED(hr) || buffer == nullptr) {
    if (err) *err = "cannot allocate the picture";
    return false;
  }
  BYTE* dst = nullptr;
  DWORD max_len = 0;
  DWORD cur_len = 0;
  if (FAILED(buffer->Lock(&dst, &max_len, &cur_len)) || dst == nullptr) {
    buffer->Release();
    if (err) *err = "cannot lock the picture";
    return false;
  }
  std::memcpy(dst, frame.data.data(), size);
  buffer->Unlock();

  IMFSample* sample = nullptr;
  hr = MFCreateSample(&sample);
  if (SUCCEEDED(hr)) hr = sample->AddBuffer(buffer);
  if (SUCCEEDED(hr)) {
    // Media Foundation counts in 100-nanosecond units.
    sample->SetSampleTime(timestamp_ms * 10000LL);
    sample->SetSampleDuration(10000000LL / impl_->fps);
    hr = impl_->transform->ProcessInput(0, sample, 0);
  }
  buffer->Release();
  if (sample) sample->Release();
  if (FAILED(hr)) {
    if (err) *err = "the encoder refused the picture";
    return false;
  }

  for (;;) {
    IMFSample* out_sample = nullptr;
    const Pull what = pull_sample(impl_->transform, &out_sample);
    if (what == Pull::kNeedMore) break;
    if (what == Pull::kError) {
      if (err) *err = "the encoder failed while producing the bitstream";
      return false;
    }
    sample_bytes(out_sample, out);
    out_sample->Release();
  }
  return true;
}

void H264Encoder::stop() {
  if (impl_ && impl_->transform) {
    impl_->transform->Release();
    impl_->transform = nullptr;
  }
  running_ = false;
}

#else  // !_WIN32

bool H264Encoder::start(int width, int height, int fps, int bitrate, std::string* err) {
  (void)width;
  (void)height;
  (void)fps;
  (void)bitrate;
  if (err) *err = "there is no H.264 encoder on this platform";
  return false;
}

bool H264Encoder::encode(const YuvFrame& frame, int64_t timestamp_ms, std::string* out,
                         std::string* err) {
  (void)frame;
  (void)timestamp_ms;
  (void)out;
  if (err) *err = "there is no H.264 encoder on this platform";
  return false;
}

void H264Encoder::stop() { running_ = false; }

#endif  // _WIN32

struct H264Decoder::Impl {
#ifdef _WIN32
  MfGuard mf;
  IMFTransform* transform = nullptr;
  int width = 0;
  int height = 0;
#endif
};

H264Decoder::H264Decoder() : impl_(new Impl()) {}
H264Decoder::~H264Decoder() { stop(); }

#ifdef _WIN32

namespace {

/// Configure one candidate decoder for H.264 in and NV12 out.
bool configure_decoder(IMFTransform* transform, int width, int height, std::string* err) {
  IMFMediaType* in = nullptr;
  MFCreateMediaType(&in);
  in->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
  in->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_H264);
  set_frame_size(in, width, height);
  in->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);

  IMFMediaType* out = nullptr;
  MFCreateMediaType(&out);
  out->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
  out->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12);
  set_frame_size(out, width, height);
  out->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
  out->SetUINT32(MF_MT_ALL_SAMPLES_INDEPENDENT, TRUE);

  HRESULT hr = transform->SetInputType(0, in, 0);
  if (SUCCEEDED(hr)) hr = transform->SetOutputType(0, out, 0);
  if (FAILED(hr)) {
    hr = transform->SetOutputType(0, out, 0);
    if (SUCCEEDED(hr)) hr = transform->SetInputType(0, in, 0);
  }
  safe_release(in);
  safe_release(out);
  if (FAILED(hr)) {
    if (err) *err = "the decoder refused H.264 in / NV12 out";
    return false;
  }
  return true;
}

/// Copy one NV12 picture out of a media buffer.
bool copy_nv12(IMFMediaBuffer* buffer, int width, int height, YuvFrame* out) {
  if (width <= 0 || height <= 0) return false;
  const std::size_t y_size = static_cast<std::size_t>(width) * height;
  const std::size_t uv_size = y_size / 4;
  out->width = width;
  out->height = height;
  out->data.assign(y_size + uv_size * 2, 0);

  BYTE* bytes = nullptr;
  DWORD len = 0;
  DWORD max_len = 0;
  if (FAILED(buffer->Lock(&bytes, &max_len, &len)) || bytes == nullptr) return false;
  if (len < y_size + uv_size * 2) {
    buffer->Unlock();
    return false;
  }
  std::memcpy(out->data.data(), bytes, y_size);
  std::memcpy(out->data.data() + y_size, bytes + y_size, uv_size);
  std::memcpy(out->data.data() + y_size + uv_size, bytes + y_size + uv_size, uv_size);
  buffer->Unlock();
  return true;
}

}  // namespace

bool H264Decoder::start(int width_hint, int height_hint, std::string* err) {
  if (running_) return true;
  if (!impl_->mf.ok) {
    if (err) *err = "Media Foundation did not start";
    return false;
  }

  IMFActivate** activates = nullptr;
  UINT32 count = 0;
  HRESULT hr = MFTEnumEx(MFT_CATEGORY_VIDEO_DECODER, 0, nullptr, nullptr, &activates, &count);
  if (FAILED(hr) || activates == nullptr || count == 0) {
    if (err) *err = "this Windows has no H.264 decoder";
    return false;
  }

  std::string last_error = "no decoder accepted the stream";
  for (UINT32 i = 0; i < count; i++) {
    IMFTransform* transform = nullptr;
    if (FAILED(activates[i]->ActivateObject(IID_IMFTransform, reinterpret_cast<void**>(&transform))) ||
        transform == nullptr) {
      continue;
    }
    std::string why;
    if (configure_decoder(transform, width_hint, height_hint, &why)) {
      impl_->transform = transform;
      impl_->width = width_hint;
      impl_->height = height_hint;
      running_ = true;
      CoTaskMemFree(activates);
      return true;
    }
    last_error = why;
    transform->Release();
  }
  CoTaskMemFree(activates);
  if (err) *err = last_error;
  return false;
}

bool H264Decoder::decode(const uint8_t* data, std::size_t len, std::vector<YuvFrame>* out,
                         std::string* err) {
  if (!running_ || impl_->transform == nullptr) {
    if (err) *err = "the decoder is not running";
    return false;
  }
  if (data == nullptr || len == 0) return true;

  IMFMediaBuffer* buffer = nullptr;
  HRESULT hr = MFCreateMemoryBuffer(static_cast<DWORD>(len), &buffer);
  if (FAILED(hr) || buffer == nullptr) {
    if (err) *err = "cannot allocate the bitstream";
    return false;
  }
  BYTE* dst = nullptr;
  DWORD max_len = 0;
  DWORD cur_len = 0;
  if (FAILED(buffer->Lock(&dst, &max_len, &cur_len)) || dst == nullptr) {
    buffer->Release();
    if (err) *err = "cannot lock the bitstream";
    return false;
  }
  std::memcpy(dst, data, len);
  buffer->Unlock();

  IMFSample* sample = nullptr;
  hr = MFCreateSample(&sample);
  if (SUCCEEDED(hr)) hr = sample->AddBuffer(buffer);
  if (SUCCEEDED(hr)) hr = impl_->transform->ProcessInput(0, sample, 0);
  buffer->Release();
  if (sample) sample->Release();
  if (FAILED(hr)) {
    if (err) *err = "the decoder refused the bitstream";
    return false;
  }

  for (;;) {
    IMFSample* out_sample = nullptr;
    const Pull what = pull_sample(impl_->transform, &out_sample);
    if (what == Pull::kNeedMore) break;
    if (what == Pull::kError) {
      if (err) *err = "the decoder failed while producing the picture";
      return false;
    }
    // The size comes from the output type: the decoder decides it.
    int width = impl_->width;
    int height = impl_->height;
    IMFMediaType* type = nullptr;
    if (SUCCEEDED(impl_->transform->GetOutputCurrentType(0, &type)) && type != nullptr) {
      UINT32 packed = 0;
      if (SUCCEEDED(type->GetUINT32(MF_MT_FRAME_SIZE, &packed))) {
        split_frame_size(packed, &width, &height);
      }
      type->Release();
    }
    IMFMediaBuffer* media = nullptr;
    if (SUCCEEDED(out_sample->ConvertToContiguousBuffer(&media)) && media != nullptr) {
      YuvFrame frame;
      if (copy_nv12(media, width, height, &frame)) out->push_back(frame);
      media->Release();
    }
    out_sample->Release();
  }
  return true;
}

void H264Decoder::stop() {
  if (impl_ && impl_->transform) {
    impl_->transform->Release();
    impl_->transform = nullptr;
  }
  running_ = false;
}

#else  // !_WIN32

bool H264Decoder::start(int width_hint, int height_hint, std::string* err) {
  (void)width_hint;
  (void)height_hint;
  if (err) *err = "there is no H.264 decoder on this platform";
  return false;
}

bool H264Decoder::decode(const uint8_t* data, std::size_t len, std::vector<YuvFrame>* out,
                         std::string* err) {
  (void)data;
  (void)len;
  (void)out;
  if (err) *err = "there is no H.264 decoder on this platform";
  return false;
}

void H264Decoder::stop() { running_ = false; }

#endif  // _WIN32

}  // namespace tea_tunnel
