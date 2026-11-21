#!/usr/bin/env python3
"""Same persistent protocol and CPU controls, explicitly returning scalar counts."""
import benchmark_stacks as b
original=b.command

def command(engine,file,spec,tab,samples,target,cpus):
    cmd=original(engine,file,spec,tab,samples,target,cpus)
    if engine in ('rust-paged','rust-safe','rust-mmap'):cmd[0]=str(b.REF/'counts/target/release/count-reader')
    elif engine in ('python','python-current'):cmd[1]=str(b.ROOT/'tools/stack_benchmark/count_python.py')
    elif engine in ('java','java-current'):cmd[cmd.index('CorpusJava')]='CorpusCountJava'
    elif engine in ('clojure','clojure-current'):cmd[cmd.index(str(b.ROOT/'tools/stack_benchmark/corpus_clojure.clj'))]=str(b.ROOT/'tools/stack_benchmark/count_clojure.clj')
    elif engine=='dotnet':cmd[1]=str(b.REF/'counts/net/CountNet.dll')
    elif engine=='cpp':cmd[0]=str(b.REF/'counts/cpp-count')
    elif engine=='rust-ffi':cmd[0]=str(b.REF/'sources/Sifir-io__geodesk-rs/target/release/examples/count-corpus')
    elif engine=='php-ffi':cmd[-1]='count'
    elif engine=='php':
        old=f'type=bind,src={b.ROOT}/tools/stack_benchmark/corpus_php.php,dst=/benchmark/worker.php,readonly'
        cmd[cmd.index(old)]=f'type=bind,src={b.REF}/counts/count_php.php,dst=/benchmark/worker.php,readonly'
    return cmd
b.command=command
if __name__=='__main__':
    import sys
    if '--spec' not in sys.argv:
        sys.argv.extend(['--spec',str(b.ROOT/'benchmarks/uk-corpus-counts.json')])
    b.main(consumer='scalar_count')
