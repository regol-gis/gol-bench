"""Bind a whole-file digest to one immutable local inode during a campaign."""
import hashlib
import os
import time


def stamp(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def unchanged(path, identity):
    if str(path.resolve()) != identity['path'] or stamp(path.stat()) != identity['stat']:
        raise ValueError(f'benchmark input changed: {path}')
    # Filesystems can give a newly written inode identical timestamps for
    # several writes in one clock tick. Re-read these inputs on every reuse.
    if identity.get('rehash_on_reuse',True):
        with path.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=identity['sha256']:
                raise ValueError(f'benchmark input changed: {path}')


def identify(path, checked_digest=None):
    if checked_digest is None:
        with path.open('rb') as stream:
            stat=os.fstat(stream.fileno())
            digest=hashlib.file_digest(stream,'sha256').hexdigest()
    else:
        stat=path.stat()
        digest=checked_digest(path)

    recently_written=max(stat.st_ctime_ns,stat.st_mtime_ns) >= time.time_ns()-2_000_000_000
    identity={'path':str(path.resolve()),'stat':stamp(stat),'sha256':digest,
              'rehash_on_reuse':stat.st_size <= 1<<20 or recently_written}
    unchanged(path,identity)
    return identity
