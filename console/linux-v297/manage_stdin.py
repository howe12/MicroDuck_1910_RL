"""Separate sudo authentication from a management command's exact stdin.

sudo -S may consume zero or one password line. The NUL-delimited header
cannot be a legal password, so the privileged wrapper can discard a leftover
authentication line without interpreting either passwords or business JSON.
"""

MAGIC = "\0MICRODUCK-MANAGE-STDIN-V1\0"
MAX_INPUT_BYTES = 512 * 1024
MAX_PASSWORD_CHARS = 256
MAX_AUTH_BYTES = 4 * MAX_PASSWORD_CHARS


def framed_input(data, password=None):
    """Return the bounded UTF-8 wire frame; errors never include its contents."""
    if not isinstance(data, str):
        raise ValueError("管理输入格式无效")
    if password is not None and (not isinstance(password, str)
            or not 0 < len(password) <= MAX_PASSWORD_CHARS
            or any(c in password for c in "\r\n\0")):
        raise ValueError("Zero sudo 密码格式无效")
    try:
        size = len(data.encode("utf-8"))
        if password is not None and len(password.encode("utf-8")) > MAX_AUTH_BYTES:
            raise ValueError("Zero sudo 密码格式无效")
    except UnicodeError:
        raise ValueError("管理输入编码无效") from None
    if size > MAX_INPUT_BYTES:
        raise ValueError("管理输入超出允许大小")
    return (password + "\n" if password is not None else "") + MAGIC + str(size) + "\n" + data


# Sent as fixed code, never containing a credential or business input. A memfd
# replaces stdin before exec, preserving the original PID, streams and exit code.
# No child is launched until the entire frame (including EOF) is validated.
WRAPPER_SOURCE = r'''
import os
import sys

MAGIC = b"\0MICRODUCK-MANAGE-STDIN-V1\0"
MAX_INPUT_BYTES = 524288
MAX_AUTH_BYTES = 1024

def exact(stream, size):
    parts = []
    while size:
        part = stream.read(size)
        if not part:
            raise ValueError()
        parts.append(part)
        size -= len(part)
    return b"".join(parts)

def main():
    stream = sys.stdin.buffer
    first = exact(stream, 1)
    if first != b"\0":
        # The optional password line was not consumed by sudo. Read bytes only;
        # do not parse, decode, echo or forward any part of this line.
        count = 0
        while first != b"\n":
            count += 1
            if count > MAX_AUTH_BYTES:
                raise ValueError()
            first = exact(stream, 1)
        first = exact(stream, 1)
    if first != b"\0" or first + exact(stream, len(MAGIC) - 1) != MAGIC:
        raise ValueError()
    header = stream.readline(16)
    if not header.endswith(b"\n") or not header[:-1].isdigit():
        raise ValueError()
    size = int(header[:-1])
    if size > MAX_INPUT_BYTES or str(size).encode("ascii") + b"\n" != header:
        raise ValueError()
    payload = exact(stream, size)
    if stream.read(1) or len(sys.argv) < 2:
        raise ValueError()
    descriptor = os.memfd_create("microduck-manage-input", os.MFD_CLOEXEC)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError()
            offset += written
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.dup2(descriptor, 0, inheritable=True)
    finally:
        if descriptor != 0:
            os.close(descriptor)
    os.execvp(sys.argv[1], sys.argv[1:])

try:
    main()
except BaseException:
    sys.stderr.write("管理输入帧无效或无法执行，命令未重发\n")
    sys.exit(125)
'''
