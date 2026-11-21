"""Identify executable and adapter inputs before measuring or resuming a stack."""
import hashlib
from pathlib import Path
import shutil
import subprocess

from paths import SDK


def identities(commands):
    digests = {}
    def fingerprint(path):
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f'missing benchmark dependency: {path}')
        key = str(path.resolve())
        if key not in digests:
            with path.open('rb') as src:
                digests[key] = hashlib.file_digest(src,'sha256').hexdigest()
        return key

    result = {}
    for engine,cmd in commands.items():
        binary = shutil.which(str(cmd[0]))
        if not binary:
            raise FileNotFoundError(f'{engine}: missing {cmd[0]}; run gol-bench setup for this adapter')
        files = [fingerprint(binary)]
        if engine in ('python','python-current'):
            script = ('import geodesk, shapely; from geodesk import _geodesk; '
                      'print(geodesk.__file__); print(_geodesk.__file__); print(shapely.lib.__file__)')
            extensions = subprocess.check_output([binary,'-c',script],text=True).splitlines()
            files.extend(fingerprint(p) for p in [cmd[1],*extensions])
        elif engine.startswith(('java','clojure')):
            import os
            for entry in cmd[cmd.index('-cp')+1].split(os.pathsep):
                path = Path(entry)
                if path.name == '*':
                    jars = sorted(path.parent.glob('*.jar'))
                    if (path.parent.name in ('java-current','java-legacy')) and not jars:
                        raise FileNotFoundError(f'{engine}: no jars in {path.parent}')
                    files.extend(fingerprint(jar) for jar in jars)
                else:
                    files.append(fingerprint(path/'CorpusJava.class'))
                    files.extend(fingerprint(p) for p in sorted(path.glob('*.class')) if p.name!='CorpusJava.class')
        elif engine == 'dotnet':
            files.extend(fingerprint(p) for p in (cmd[1],Path(cmd[1]).with_name('GeoDesk.dll')))
        elif engine == 'php-ffi':
            files.append(fingerprint(SDK/'php-ffi/libgol_php_ffi.so'))
            image = subprocess.check_output(
                ['docker', 'image', 'inspect', 'gol-benchmark-php-ffi:local', '--format', '{{.Id}}'],
                text=True
            ).strip()
            result[engine] = {'image_id': image}
        elif engine == 'php':
            files.append(fingerprint(SDK/'php-source/modules/libgeodesk.so'))
            image=subprocess.check_output(['docker','image','inspect','gol-benchmark-php-toolchain:local','--format','{{.Id}}'],text=True).strip()
            result[engine]={'image_id':image}
        result.setdefault(engine,{})['sha256']={key:digests[key] for key in files}
    return result
