// http_gzip.h -- when an answer may be compressed, and with what.
//
// The tunnel (a2_tunnel) carries these very HTTP bytes over a link that is
// often a phone or a VPN: the bytes that do not have to travel are the bytes
// that do not have to be paid for. So the server compresses what the client
// is willing to decompress - and only that.
//
// The rules live here, away from httplib: they are small, they are worth
// testing on their own, and every route has to answer the same way.
#pragma once

#include <string>

namespace tea {

/// True when the `Accept-Encoding` header allows a gzip body.
///
/// The header is a list of codings with optional weights (`gzip;q=0.8`). A
/// coding with `q=0` is not acceptable, and `*` stands for "anything I did
/// not name" - so `*` alone is enough, but `gzip;q=0, *` is not: the named
/// coding wins over the wildcard.
bool accepts_gzip(const std::string& accept_encoding);

/// True when a body of this content type is worth compressing.
///
/// Text and JSON are; images, archives and other already-compressed payloads
/// are not - they only grow. `text/event-stream` is not either: it is a live
/// stream, and it is compressed by the stream itself (the SSE routes), not
/// here.
bool content_type_compressible(const std::string& content_type);

}  // namespace tea
