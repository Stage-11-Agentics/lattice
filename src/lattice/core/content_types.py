"""An attachment's content type, from Lattice's own table.

The type is guessed from the payload's filename by the stdlib ``mimetypes``
algorithm (compression suffixes first, then an encoding suffix, then the type
by lowercased suffix), over the literal name (never parsed as a URL) and the
frozen tables below rather than ``mimetypes``' live ones. The live tables
differ by interpreter (3.12.0 has no ``.md``; 3.14 adds and changes entries)
and are extended by host files such as ``/etc/apache2/mime.types`` and
``/etc/mime.types``, or the registry on Windows, which differ between machines.
An artifact's metadata is board bytes, so the same attach must store the same
type wherever it runs: locally, or on a server (LAT-356).

``TYPES`` is CPython 3.13's built-in strict ``types_map``: a superset of 3.12's
with no entry changed. Change it only as a declared change (SPEC G-6).
"""

from __future__ import annotations

import posixpath
from types import MappingProxyType

SUFFIXES = MappingProxyType(
    {
        ".svgz": ".svg.gz",
        ".tgz": ".tar.gz",
        ".taz": ".tar.gz",
        ".tz": ".tar.gz",
        ".tbz2": ".tar.bz2",
        ".txz": ".tar.xz",
    }
)

ENCODINGS = MappingProxyType(
    {".gz": "gzip", ".Z": "compress", ".bz2": "bzip2", ".xz": "xz", ".br": "br"}
)

TYPES = MappingProxyType(
    {
        ".3g2": "audio/3gpp2",
        ".3gp": "audio/3gpp",
        ".3gpp": "audio/3gpp",
        ".3gpp2": "audio/3gpp2",
        ".a": "application/octet-stream",
        ".aac": "audio/aac",
        ".adts": "audio/aac",
        ".ai": "application/postscript",
        ".aif": "audio/x-aiff",
        ".aifc": "audio/x-aiff",
        ".aiff": "audio/x-aiff",
        ".ass": "audio/aac",
        ".au": "audio/basic",
        ".avi": "video/x-msvideo",
        ".avif": "image/avif",
        ".bat": "text/plain",
        ".bcpio": "application/x-bcpio",
        ".bin": "application/octet-stream",
        ".bmp": "image/bmp",
        ".c": "text/plain",
        ".cdf": "application/x-netcdf",
        ".cpio": "application/x-cpio",
        ".csh": "application/x-csh",
        ".css": "text/css",
        ".csv": "text/csv",
        ".dll": "application/octet-stream",
        ".doc": "application/msword",
        ".dot": "application/msword",
        ".dvi": "application/x-dvi",
        ".eml": "message/rfc822",
        ".eps": "application/postscript",
        ".etx": "text/x-setext",
        ".exe": "application/octet-stream",
        ".gif": "image/gif",
        ".gtar": "application/x-gtar",
        ".h": "text/plain",
        ".h5": "application/x-hdf5",
        ".hdf": "application/x-hdf",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".htm": "text/html",
        ".html": "text/html",
        ".ico": "image/vnd.microsoft.icon",
        ".ief": "image/ief",
        ".jpe": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".jpg": "image/jpeg",
        ".js": "text/javascript",
        ".json": "application/json",
        ".ksh": "text/plain",
        ".latex": "application/x-latex",
        ".loas": "audio/aac",
        ".m1v": "video/mpeg",
        ".m3u": "application/vnd.apple.mpegurl",
        ".m3u8": "application/vnd.apple.mpegurl",
        ".man": "application/x-troff-man",
        ".markdown": "text/markdown",
        ".md": "text/markdown",
        ".me": "application/x-troff-me",
        ".mht": "message/rfc822",
        ".mhtml": "message/rfc822",
        ".mif": "application/x-mif",
        ".mjs": "text/javascript",
        ".mov": "video/quicktime",
        ".movie": "video/x-sgi-movie",
        ".mp2": "audio/mpeg",
        ".mp3": "audio/mpeg",
        ".mp4": "video/mp4",
        ".mpa": "video/mpeg",
        ".mpe": "video/mpeg",
        ".mpeg": "video/mpeg",
        ".mpg": "video/mpeg",
        ".ms": "application/x-troff-ms",
        ".n3": "text/n3",
        ".nc": "application/x-netcdf",
        ".nq": "application/n-quads",
        ".nt": "application/n-triples",
        ".nws": "message/rfc822",
        ".o": "application/octet-stream",
        ".obj": "application/octet-stream",
        ".oda": "application/oda",
        ".opus": "audio/opus",
        ".p12": "application/x-pkcs12",
        ".p7c": "application/pkcs7-mime",
        ".pbm": "image/x-portable-bitmap",
        ".pdf": "application/pdf",
        ".pfx": "application/x-pkcs12",
        ".pgm": "image/x-portable-graymap",
        ".pl": "text/plain",
        ".png": "image/png",
        ".pnm": "image/x-portable-anymap",
        ".pot": "application/vnd.ms-powerpoint",
        ".ppa": "application/vnd.ms-powerpoint",
        ".ppm": "image/x-portable-pixmap",
        ".pps": "application/vnd.ms-powerpoint",
        ".ppt": "application/vnd.ms-powerpoint",
        ".ps": "application/postscript",
        ".pwz": "application/vnd.ms-powerpoint",
        ".py": "text/x-python",
        ".pyc": "application/x-python-code",
        ".pyo": "application/x-python-code",
        ".qt": "video/quicktime",
        ".ra": "audio/x-pn-realaudio",
        ".ram": "application/x-pn-realaudio",
        ".ras": "image/x-cmu-raster",
        ".rdf": "application/xml",
        ".rgb": "image/x-rgb",
        ".roff": "application/x-troff",
        ".rst": "text/x-rst",
        ".rtf": "text/rtf",
        ".rtx": "text/richtext",
        ".sgm": "text/x-sgml",
        ".sgml": "text/x-sgml",
        ".sh": "application/x-sh",
        ".shar": "application/x-shar",
        ".snd": "audio/basic",
        ".so": "application/octet-stream",
        ".src": "application/x-wais-source",
        ".srt": "text/plain",
        ".sv4cpio": "application/x-sv4cpio",
        ".sv4crc": "application/x-sv4crc",
        ".svg": "image/svg+xml",
        ".swf": "application/x-shockwave-flash",
        ".t": "application/x-troff",
        ".tar": "application/x-tar",
        ".tcl": "application/x-tcl",
        ".tex": "application/x-tex",
        ".texi": "application/x-texinfo",
        ".texinfo": "application/x-texinfo",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".tr": "application/x-troff",
        ".trig": "application/trig",
        ".tsv": "text/tab-separated-values",
        ".txt": "text/plain",
        ".ustar": "application/x-ustar",
        ".vcf": "text/x-vcard",
        ".vtt": "text/vtt",
        ".wasm": "application/wasm",
        ".wav": "audio/x-wav",
        ".webm": "video/webm",
        ".webmanifest": "application/manifest+json",
        ".webp": "image/webp",
        ".wiz": "application/msword",
        ".wsdl": "application/xml",
        ".xbm": "image/x-xbitmap",
        ".xlb": "application/vnd.ms-excel",
        ".xls": "application/vnd.ms-excel",
        ".xml": "text/xml",
        ".xpdl": "application/xml",
        ".xpm": "image/x-xpixmap",
        ".xsl": "application/xml",
        ".xwd": "image/x-xwindowdump",
        ".zip": "application/zip",
    }
)


def guess_content_type(filename: str) -> str | None:
    """The content type of a payload named *filename*, or ``None`` when the
    table has no entry for its suffix."""
    base, ext = posixpath.splitext(filename)
    while ext.lower() in SUFFIXES:
        base, ext = posixpath.splitext(base + SUFFIXES[ext.lower()])
    if ext in ENCODINGS:  # case-sensitive, as in mimetypes
        base, ext = posixpath.splitext(base)
    return TYPES.get(ext.lower())
