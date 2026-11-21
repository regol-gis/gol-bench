<?php
// Persistent PHP CLI adapter for the pinned public C++ SDK through FFI.
declare(strict_types=1);

function checkedPointer(FFI $api, ?FFI\CData $pointer): FFI\CData
{
    if ($pointer === null || FFI::isNull($pointer)) {
        throw new RuntimeException($api->gol_php_error());
    }
    return $pointer;
}

function multiply64(int $left, int $right): int
{
    // Four 16-bit limbs retain the low 64 bits without PHP float promotion.
    $product = 0;
    $carry = 0;
    for ($limb = 0; $limb < 4; ++$limb) {
        for ($part = 0; $part <= $limb; ++$part) {
            $a = ($left >> (16 * $part)) & 0xFFFF;
            $b = ($right >> (16 * ($limb - $part))) & 0xFFFF;
            $carry += $a * $b;
        }
        $product |= ($carry & 0xFFFF) << (16 * $limb);
        $carry >>= 16;
    }
    return $product;
}

function identityHash(int $typedId): int
{
    $result = $typedId ^ ($typedId >> 30);
    $result = multiply64($result, (0xBF58476D << 32) | 0x1CE4E5B9);
    $result ^= ($result >> 27) & (PHP_INT_MAX >> 26);
    $result = multiply64($result, (0x94D049BB << 32) | 0x133111EB);
    return $result ^ (($result >> 31) & (PHP_INT_MAX >> 30));
}

function scan(FFI $api, FFI\CData $query, bool $verify): int|array
{
    $iterator = checkedPointer($api, $api->gol_php_iterate($query));
    $feature = $api->new('GolPhpFeature');
    $address = FFI::addr($feature);
    $count = $sum = $xor = 0;
    try {
        while (($status = $api->gol_php_next($iterator, $address)) > 0) {
            $id = $feature->id;
            $type = $feature->type;
            ++$count;
            if ($verify) {
                $mixed = identityHash($id * 4 + $type);
                $low = ($sum & 0xFFFFFFFF) + ($mixed & 0xFFFFFFFF);
                $high = ($sum >> 32) + ($mixed >> 32) + ($low >> 32);
                $sum = ($high << 32) | ($low & 0xFFFFFFFF);
                $xor ^= $mixed;
            }
        }
        if ($status < 0) {
            throw new RuntimeException($api->gol_php_error());
        }
    } finally {
        $api->gol_php_release_iterator($iterator);
    }
    return $verify ? ['count' => $count, 'sum' => sprintf('%u', $sum), 'xor' => sprintf('%u', $xor)] : $count;
}

function nativeCount(FFI $api, FFI\CData $query): int
{
    $result = $api->gol_php_count($query);
    if ($result < 0) {
        throw new RuntimeException($api->gol_php_error());
    }
    return $result;
}

function emit(array $row): void
{
    echo json_encode($row, JSON_THROW_ON_ERROR | JSON_INVALID_UTF8_SUBSTITUTE), "\n";
}

[$script, $file, $spec, $samples, $targetMs, $mode] = $argv;
if (PHP_INT_SIZE !== 8) {
    throw new RuntimeException('The PHP FFI adapter requires 64-bit integers');
}
$api = FFI::cdef(file_get_contents(__DIR__ . '/php_ffi_api.h'), __DIR__ . '/libgol_php_ffi.so');
$start = hrtime(true);
$library = checkedPointer($api, $api->gol_php_open($file));
$open = hrtime(true) - $start;
try {
    foreach (json_decode(file_get_contents($spec), true, flags: JSON_THROW_ON_ERROR) as $case) {
        $name = $case['name'];
        $operation = $case['operation'] ?? 'iterate';
        fwrite(STDERR, "BEGIN $name\n");
        if ($operation !== 'iterate' || isset($case['limit']) || isset($case['steps'])) {
            emit(['engine' => 'php-ffi', 'name' => $name, 'error' => 'unsupported_equivalent_payload']);
            continue;
        }
        try {
            $bounds = null;
            if (isset($case['bounds'])) {
                if (count($case['bounds']) !== 4) {
                    throw new RuntimeException('expected four projected bbox coordinates');
                }
                $bounds = $api->new('int32_t[4]');
                foreach ($case['bounds'] as $index => $coordinate) {
                    if (!is_int($coordinate) || $coordinate < -2147483648 || $coordinate > 2147483647) {
                        throw new RuntimeException('bbox coordinate exceeds a signed 32-bit integer');
                    }
                    $bounds[$index] = $coordinate;
                }
            }
            $start = hrtime(true);
            $query = checkedPointer($api, $api->gol_php_prepare($library, $case['query'], $bounds));
            $setup = hrtime(true) - $start;
            try {
                $execute = $mode === 'count'
                    ? static fn() => nativeCount($api, $query)
                    : static fn() => scan($api, $query, false);
                $start = hrtime(true);
                $count = $execute();
                $first = hrtime(true) - $start;
                $execute();
                $start = hrtime(true);
                $execute();
                $repeats = (int)max(1, min(1000, (int)$targetMs * 1_000_000 / max(1, hrtime(true) - $start)));
                $wall = [];
                for ($sample = 0; $sample < (int)$samples; ++$sample) {
                    $start = hrtime(true);
                    for ($repeat = 0; $repeat < $repeats; ++$repeat) {
                        if ($execute() !== $count) {
                            throw new RuntimeException('unstable consumption');
                        }
                    }
                    $wall[] = (hrtime(true) - $start) / $repeats;
                }
                $identity = $mode === 'count' ? null : scan($api, $query, true);
                if ($identity !== null && $identity['count'] !== $count) {
                    throw new RuntimeException('inconsistent identity count');
                }
                $row = [
                    'engine' => 'php-ffi', 'name' => $name, 'operation' => $operation,
                    'count' => $count, 'items' => $count,
                    'open_ns' => $open, 'setup_ns' => $setup, 'first_ns' => $first,
                    'repeats' => $repeats, 'samples_wall_ns' => $wall,
                    'validation_level' => $mode === 'count' ? 'scalar_count_only' : 'typed_identities',
                    'payload' => $mode === 'count' ? 'scalar_count' : 'php_iterated_typed_id_rows',
                ];
                if ($identity !== null) {
                    $row['fingerprint'] = $identity;
                }
                emit($row);
            } finally {
                $api->gol_php_release_query($query);
            }
        } catch (Throwable $error) {
            emit(['engine' => 'php-ffi', 'name' => $name, 'error' => $error->getMessage()]);
        }
    }
} finally {
    $api->gol_php_close($library);
}
