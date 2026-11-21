"""Read one GOL in a fresh GeoDesk Python process and fingerprint selected features."""
import argparse
import hashlib
import json
from pathlib import Path

MASK = (1 << 128) - 1
CONTENTS = ('identities', 'tags', 'typed-tags', 'geometry')


def content_payload(feature, content):
    kind = 0 if feature.is_node else 1 if feature.is_way else 2
    identity = (feature.id << 2) | kind
    if content == 'identities':
        return identity.to_bytes(8, 'little')
    if content == 'geometry':
        return identity.to_bytes(8, 'little') + feature.shape.normalize().wkb
    tags = list(feature.tags)
    if content == 'tags':
        tags = [(key, str(value)) for key, value in tags]
    return json.dumps([identity, sorted(tags)], ensure_ascii=False, separators=(',', ':')).encode()


def signatures(path, queries, content):
    from geodesk import Box, Features

    library = Features(str(path))
    results = []
    for item in queries:
        features = library(item['query'])
        if 'bounds' in item:
            features = features(Box(*item['bounds']))
        count = total = xor = 0
        for feature in features:
            payload = content_payload(feature, content)
            value = int.from_bytes(hashlib.blake2b(payload, digest_size=16).digest(), 'big')
            count += 1
            total = (total + value) & MASK
            xor ^= value
        results.append(dict(name=item['name'], count=count, sum=f'{total:032x}', xor=f'{xor:032x}'))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('gol', type=Path)
    parser.add_argument('spec', type=Path)
    parser.add_argument('content', choices=CONTENTS)
    args = parser.parse_args()
    print(json.dumps(signatures(args.gol, json.loads(args.spec.read_bytes()), args.content)))


if __name__ == '__main__':
    main()
