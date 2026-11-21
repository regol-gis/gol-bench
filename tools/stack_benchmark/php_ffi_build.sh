#!/bin/sh
set -eu

cmake -S /work/libgeodesk -B /work/build \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    -DGEODESK_WITH_GEOS=OFF -DFETCHCONTENT_SOURCE_DIR_GTL=/work/gtl \
    -DFETCHCONTENT_SOURCE_DIR_CATCH2=/work/catch2
cmake --build /work/build --target geodesk -j "$1"
g++ -std=c++20 -O3 -DNDEBUG -fPIC -shared -I/work/libgeodesk/include -I/work/gtl/include \
    /adapters/php_ffi.cpp /work/build/libgeodesk.a -pthread -o /work/libgol_php_ffi.so
