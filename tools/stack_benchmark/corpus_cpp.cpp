// Original benchmark adapter for the public C++ SDK. Test-only linkage.
#include <geodesk/geodesk.h>
#include <chrono>
#include <time.h>
#include <fstream>
#include <iostream>
#include <sstream>
#include <vector>
#include <algorithm>
#include "geometry_cpp.h"
using namespace geodesk;
using Clock=std::chrono::steady_clock;
static double ns(Clock::time_point start){return std::chrono::duration<double,std::nano>(Clock::now()-start).count();}
static double cpu(){timespec t{};clock_gettime(CLOCK_PROCESS_CPUTIME_ID,&t);return double(t.tv_sec)*1e9+t.tv_nsec;}
static uint64_t mix(uint64_t x){x=(x^(x>>30))*0xbf58476d1ce4e5b9ULL;x=(x^(x>>27))*0x94d049bb133111ebULL;return x^(x>>31);}
template<class T> inline void blackhole(const T& value){asm volatile("" : : "g"(&value) : "memory");}
struct Work {uint64_t n=0,items=0,sum=0,xor_=0;};
static Work consume(const Features& q,const std::string& op,bool verify=false){Work w;for(Feature f:q){w.n++;if(op=="iterate"){blackhole(f);w.items++;if(verify){auto v=uint64_t(f.id())*4+(f.isNode()?0:f.isWay()?1:2);auto m=mix(v);w.sum+=m;w.xor_^=m;}}else if(op=="tags"){for(Tag t:f.tags()){auto k=t.key();auto v=t.value();blackhole(k);blackhole(v);w.items++;}}else if(op=="geometry"){auto g=f.toGeometry(geoctx);if(!g)throw std::runtime_error("geometry construction failed");w.items++;if(verify){auto parent=uint64_t(f.id())*4+(f.isNode()?0:f.isWay()?1:2);auto m=mix(parent^mix(geometry(g)));w.sum+=m;w.xor_^=m;}blackhole(g);GEOSGeom_destroy_r(geoctx,g);}else throw std::runtime_error("unsupported operation");}return w;}
static std::string quoted(const std::string& s){std::string o="\"";for(char c:s){if(c=='\\'||c=='\"')o+='\\';o+=c=='\n'?' ':c;}return o+'\"';}
static void array(const std::vector<double>& a){std::cout<<'[';for(size_t i=0;i<a.size();i++){if(i)std::cout<<',';std::cout<<a[i];}std::cout<<']';}
int main(int argc,char** argv){if(argc!=5)return 2;geoctx=GEOS_init_r();auto start=Clock::now();Features library(argv[1]);double open=ns(start);int samples=std::stoi(argv[3]);double target=std::stod(argv[4])*1e6;std::ifstream input(argv[2]);std::string line;
while(std::getline(input,line)){std::stringstream ss(line);std::vector<std::string>s;std::string cell;while(std::getline(ss,cell,'\t'))s.push_back(cell);auto name=s.at(0),op=s.at(1);std::cerr<<"BEGIN "<<name<<std::endl;
try{start=Clock::now();Features q=library(s.at(6).c_str());if(!s[2].empty())q=q(Box(std::stoi(s[2]),std::stoi(s[3]),std::stoi(s[4]),std::stoi(s[5])));double setup=ns(start);start=Clock::now();auto expected=consume(q,op);double first=ns(start);
if(samples==0) {
    std::cout<<"{\"name\":"<<quoted(name)<<",\"count\":"<<expected.n
             <<",\"work_items\":"<<expected.items<<",\"open_ns\":"<<open
             <<",\"setup_ns\":"<<setup<<",\"first_execution_ns\":"<<first<<"}"<<std::endl;
    continue;
}
consume(q,op);start=Clock::now();consume(q,op);int repeats=int(std::clamp(target/std::max(1.,ns(start)),1.,1000.));std::vector<double> wall,cput;
for(int i=0;i<samples;i++){double c=cpu();start=Clock::now();for(int j=0;j<repeats;j++){auto got=consume(q,op);if(got.n!=expected.n||got.items!=expected.items)throw std::runtime_error("unstable consumption");}wall.push_back(ns(start)/repeats);cput.push_back((cpu()-c)/repeats);}auto ids=consume(q,"iterate",true);auto verified=op=="geometry"?consume(q,op,true):Work{};
std::cout<<"{\"engine\":\"cpp\",\"name\":"<<quoted(name)<<",\"operation\":"<<quoted(op)<<",\"count\":"<<expected.n<<",\"items\":"<<expected.items<<",\"open_ns\":"<<open<<",\"setup_ns\":"<<setup<<",\"first_ns\":"<<first<<",\"repeats\":"<<repeats<<",\"samples_wall_ns\":";array(wall);std::cout<<",\"samples_cpu_ns\":";array(cput);std::cout<<",\"fingerprint\":{\"count\":"<<ids.n<<",\"sum\":\""<<ids.sum<<"\",\"xor\":\""<<ids.xor_<<"\"},\"work_fingerprint\":";if(op=="geometry")std::cout<<"{\"features\":"<<verified.n<<",\"items\":"<<verified.items<<",\"sum\":\""<<verified.sum<<"\",\"xor\":\""<<verified.xor_<<"\"}";else std::cout<<"null";std::cout<<",\"validation_level\":\"identities_and_consumption_counts\"}"<<std::endl;
}catch(std::exception& e){std::cout<<"{\"engine\":\"cpp\",\"name\":"<<quoted(name)<<",\"error\":"<<quoted(e.what())<<"}"<<std::endl;}}
}
