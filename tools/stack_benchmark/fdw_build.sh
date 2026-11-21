#!/bin/sh
set -eu

jobs=$1
cmake -S deps/libgeodesk -B deps/libgeodesk/build \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    -DGEODESK_WITH_GEOS=OFF \
    -DFETCHCONTENT_SOURCE_DIR_GTL=/work/deps/libgeodesk/build/_deps/gtl-src \
    -DFETCHCONTENT_SOURCE_DIR_CATCH2=/work/deps/libgeodesk/build/_deps/catch2-src
cmake --build deps/libgeodesk/build --target geodesk -j "$jobs"

(cd deps/postgis && ./configure --without-raster --without-topology \
    --without-address-standardizer --without-protobuf)
make -C deps/postgis/liblwgeom -j "$jobs"
make -C deps/postgis/libpgcommon -j "$jobs"
make -j "$jobs" with_llvm=no
