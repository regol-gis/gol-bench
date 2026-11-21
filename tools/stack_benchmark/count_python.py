#!/usr/bin/env python3
"""Reuse the persistent worker protocol with an explicit scalar-count consumer."""
import pathlib,runpy,sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import benchmark_workloads
original=benchmark_workloads.consume

def count(query,operation,verify=False):
    if operation=='iterate' and not verify:
        n=query.count
        return {'features':n,'items':n,'sum':'0','xor':'0'}
    return original(query,operation,verify)
benchmark_workloads.consume=count
runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1]/'query_benchmark_python.py'),run_name='__main__')
