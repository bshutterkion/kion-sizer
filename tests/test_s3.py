import pytest

from kion_sizer import profile


class FakeLister:
    def __init__(self, objs):
        self._objs = objs

    def list(self, bucket, prefix):
        return self._objs


def test_profile_from_lister_sums_parquet():
    objs = [
        profile.S3Obj("p/a.parquet", 1000),
        profile.S3Obj("p/b.parquet", 2000),
        profile.S3Obj("p/Manifest.json", 10),
    ]
    p = profile.profile_from_lister(FakeLister(objs), "bucket", "p/", "s3://bucket/p/")
    assert p.file_count == 2
    assert p.compressed_bytes == 3000
    assert p.format == "parquet"


def test_parse_s3_uri():
    b, k = profile.parse_s3_uri("s3://my-bucket/cur/month/")
    assert b == "my-bucket"
    assert k == "cur/month/"
    with pytest.raises(profile.ProfileError):
        profile.parse_s3_uri("https://x")


class FakeBody:
    """Mimics botocore's StreamingBody: direct iteration yields 1k *chunks*
    (not lines), but read() and iter_lines() are available.
    """

    _DEFAULT_CHUNK_SIZE = 1024

    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def __iter__(self):
        for i in range(0, len(self._data), self._DEFAULT_CHUNK_SIZE):
            yield self._data[i : i + self._DEFAULT_CHUNK_SIZE]

    def iter_lines(self, chunk_size=1024):
        lines = self._data.split(b"\n")
        if lines and lines[-1] == b"":
            lines = lines[:-1]
        for ln in lines:
            yield ln


class FakeS3Client:
    def __init__(self, files):
        self._files = files  # key -> bytes

    def get_object(self, Bucket, Key):
        return {"Body": FakeBody(self._files[Key])}


def test_sample_s3_non_gz_counts_lines_not_chunks():
    # header + 2000 data rows, well over 1 KiB so a chunk-count would be ~8, not 2000.
    data = b"h1,h2\n" + b"x,y\n" * 2000
    client = FakeS3Client({"p/a.csv": data})
    objs = [profile.S3Obj("p/a.csv", len(data))]
    est, sampled, total = profile._sample_s3_raw_rows(client, "b", objs, 3)
    assert est == 2000
    assert sampled == 1
    assert total == 1


def test_sample_s3_gz_counts_rows(tmp_path):
    import gzip

    raw = b"h1,h2\n" + b"x,y\n" * 500
    gz = gzip.compress(raw)
    client = FakeS3Client({"p/a.csv.gz": gz})
    objs = [profile.S3Obj("p/a.csv.gz", len(gz))]
    est, sampled, total = profile._sample_s3_raw_rows(client, "b", objs, 3)
    assert est == 500
