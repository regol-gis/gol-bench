// Original canonical geometry fingerprint, equivalent to benchmark_workloads.py.
#include <geos_c.h>
#include <array>
using XY=std::array<int32_t,2>;
static GEOSContextHandle_t geoctx;
static uint64_t word(uint64_t h,uint64_t v,int n){for(int i=0;i<n;i++){h=(h^(v&255))*1099511628211ULL;v>>=8;}return h;}
static uint64_t mark(char c){return (14695981039346656037ULL^uint8_t(c))*1099511628211ULL;}
static size_t rotation(const std::vector<XY>& a){size_t n=a.size(),i=0,j=1,k=0;while(i<n&&j<n&&k<n){auto x=a[(i+k)%n],y=a[(j+k)%n];if(x==y){k++;continue;}if(x>y){i+=k+1;if(i<=j)i=j+1;}else{j+=k+1;if(j<=i)j=i+1;}k=0;}return std::min(i,j);}
static uint64_t seq(char kind,const std::vector<XY>& a,size_t start){uint64_t h=word(mark(kind),a.size(),8);for(size_t i=0;i<a.size();i++){auto xy=a[(start+i)%a.size()];h=word(word(h,uint32_t(xy[0]),4),uint32_t(xy[1]),4);}return h;}
static std::vector<XY> coords(const GEOSGeometry* g){auto s=GEOSGeom_getCoordSeq_r(geoctx,g);unsigned n=0;if(!s||!GEOSCoordSeq_getSize_r(geoctx,s,&n))throw std::runtime_error("coordinate access failed");std::vector<XY>a;a.reserve(n);for(unsigned i=0;i<n;i++){double x,y;if(!GEOSCoordSeq_getXY_r(geoctx,s,i,&x,&y))throw std::runtime_error("coordinate read failed");a.push_back({int32_t(x),int32_t(y)});}return a;}
static uint64_t path(const GEOSGeometry* g,bool ring){auto a=coords(g);if(ring&&!a.empty()&&a.front()==a.back())a.pop_back();auto f=seq(ring?'R':'L',a,ring?rotation(a):0);std::reverse(a.begin(),a.end());return std::min(f,seq(ring?'R':'L',a,ring?rotation(a):0));}
static uint64_t components(char kind,std::vector<uint64_t> a){std::sort(a.begin(),a.end());auto h=word(mark(kind),a.size(),8);for(auto v:a)h=word(h,v,8);return h;}
static uint64_t geometry(const GEOSGeometry* g){int kind=GEOSGeomTypeId_r(geoctx,g);if(kind==GEOS_POINT)return seq('P',coords(g),0);if(kind==GEOS_LINESTRING)return path(g,false);if(kind==GEOS_POLYGON){std::vector<uint64_t>a;int n=GEOSGetNumInteriorRings_r(geoctx,g);for(int i=0;i<n;i++)a.push_back(path(GEOSGetInteriorRingN_r(geoctx,g,i),true));return word(word(mark('A'),path(GEOSGetExteriorRing_r(geoctx,g),true),8),components('H',a),8);}if(kind==GEOS_MULTIPOLYGON||kind==GEOS_GEOMETRYCOLLECTION){std::vector<uint64_t>a;int n=GEOSGetNumGeometries_r(geoctx,g);for(int i=0;i<n;i++)a.push_back(geometry(GEOSGetGeometryN_r(geoctx,g,i)));return components(kind==GEOS_MULTIPOLYGON?'M':'C',a);}throw std::runtime_error("unsupported geometry kind");}
