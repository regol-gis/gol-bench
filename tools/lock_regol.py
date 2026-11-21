"""Stage a compact build-input tag and update the reader lock for a Regol revision."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

from paths import ROOT
from prepare_regol import git, source_inputs, LOCK


def lock(source, revision, label):
    sha=git(source,'rev-parse','--verify','--end-of-options',revision+'^{commit}')
    tag='regol-source-'+sha[:12]
    with tempfile.TemporaryDirectory() as tmp:
        directory=Path(tmp)
        tree=directory/'tree'
        tree.mkdir()
        archive=directory/'source.tar'
        subprocess.run(['git','-C',source,'archive','-o',archive,sha,*source_inputs(source, sha)],check=True)
        with tarfile.open(archive) as snapshot:
            snapshot.extractall(tree,filter='data')
        env={**os.environ,'GIT_DIR':git(ROOT,'rev-parse','--absolute-git-dir'),
             'GIT_WORK_TREE':str(tree),'GIT_INDEX_FILE':str(directory/'index')}
        subprocess.run(['git','add','--force','--','.'],cwd=tree,env=env,check=True)
        digest=subprocess.check_output(['git','write-tree'],env=env,text=True).strip()
    ref='refs/tags/'+tag
    if subprocess.run(['git','-C',ROOT,'show-ref','--verify','--quiet',ref]).returncode==0:
        if git(ROOT,'rev-parse',ref+'^{tree}')!=digest:
            raise ValueError(f'existing source tag differs from {sha}')
    else:
        commit=git(ROOT,'commit-tree',digest,'-m','Build input from regol-gis/regol at '+sha)
        subprocess.run(['git','-C',ROOT,'tag',tag,commit],check=True)
    catalogue=json.loads(LOCK.read_bytes())
    catalogue[label]={'revision':sha,'tag':tag,'tree':digest}
    LOCK.write_text(json.dumps(catalogue,indent=2)+'\n')
    print(tag)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('revision')
    parser.add_argument('--label',default='current')
    args=parser.parse_args()
    lock(args.source,args.revision,args.label)
