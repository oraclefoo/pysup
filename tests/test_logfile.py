from pysup.logfile import RotatingFileWriter, tail_lines


def test_rotation_keeps_limited_backups(workdir):
    path = workdir / "out.log"
    writer = RotatingFileWriter(path, max_bytes=10, backups=2)
    for chunk in (b"aaaaaaaa\n", b"bbbbbbbb\n", b"cccccccc\n", b"dddddddd\n"):
        writer.write(chunk)
    writer.close()
    assert path.read_bytes() == b"dddddddd\n"
    assert (workdir / "out.log.1").read_bytes() == b"cccccccc\n"
    assert (workdir / "out.log.2").read_bytes() == b"bbbbbbbb\n"
    assert not (workdir / "out.log.3").exists()


def test_rotation_without_backups_truncates(workdir):
    path = workdir / "out.log"
    writer = RotatingFileWriter(path, max_bytes=5, backups=0)
    writer.write(b"1234\n")
    writer.write(b"5678\n")
    writer.close()
    assert path.read_bytes() == b"5678\n"
    assert list(workdir.iterdir()) == [path]


def test_appends_to_existing_file(workdir):
    path = workdir / "out.log"
    path.write_bytes(b"old\n")
    writer = RotatingFileWriter(path, max_bytes=0, backups=1)
    writer.write(b"new\n")
    writer.close()
    assert path.read_bytes() == b"old\nnew\n"


def test_tail_lines(workdir):
    path = workdir / "big.log"
    path.write_bytes(b"".join(f"line {i}\n".encode() for i in range(5000)))
    assert tail_lines(path, 3) == b"line 4997\nline 4998\nline 4999\n"
    assert tail_lines(path, 0) == b""
    small = workdir / "small.log"
    small.write_bytes(b"one\ntwo")
    assert tail_lines(small, 10) == b"one\ntwo"
