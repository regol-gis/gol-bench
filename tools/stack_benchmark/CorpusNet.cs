// Original public-API adapter. No decoder/engine implementation is included.
using System.Diagnostics;
using System.Text.Json;
using GeoDesk.Feature;
using GeoDesk.Geom;
using NetTopologySuite.Geometries;
class CorpusNet {
 static object? sink;
 static ulong Mix(ulong x){x=(x^(x>>30))*0xbf58476d1ce4e5b9UL;x=(x^(x>>27))*0x94d049bb133111ebUL;return x^(x>>31);}

 const ulong FNV=0xcbf29ce484222325UL;
 static ulong Word(ulong h,ulong v,int n){for(int i=0;i<n;i++){h=(h^(v&255))*1099511628211UL;v>>=8;}return h;}
 static ulong Mark(char c)=>(FNV^(byte)c)*1099511628211UL;
 static int Rotation(Coordinate[] a){int n=a.Length,i=0,j=1,k=0;while(i<n&&j<n&&k<n){var x=a[(i+k)%n];var y=a[(j+k)%n];int c=x.X.CompareTo(y.X);if(c==0)c=x.Y.CompareTo(y.Y);if(c==0){k++;continue;}if(c>0){i+=k+1;if(i<=j)i=j+1;}else{j+=k+1;if(j<=i)j=i+1;}k=0;}return Math.Min(i,j);}
 static ulong Seq(char kind,Coordinate[] a,int start){ulong h=Word(Mark(kind),(ulong)a.Length,8);for(int i=0;i<a.Length;i++){var xy=a[(start+i)%a.Length];h=Word(Word(h,(uint)(int)xy.X,4),(uint)(int)xy.Y,4);}return h;}
 static ulong PathHash(Coordinate[] a,bool ring){if(ring&&a.Length>0&&a[0].Equals2D(a[^1]))a=a[..^1];ulong f=Seq(ring?'R':'L',a,ring?Rotation(a):0);Array.Reverse(a);return Math.Min(f,Seq(ring?'R':'L',a,ring?Rotation(a):0));}
 static ulong Components(char kind,ulong[] a){Array.Sort(a);ulong h=Word(Mark(kind),(ulong)a.Length,8);foreach(var v in a)h=Word(h,v,8);return h;}
 static ulong GeometryHash(Geometry g){if(g is Point)return Seq('P',g.Coordinates,0);if(g is Polygon p){var a=new ulong[p.NumInteriorRings];for(int i=0;i<a.Length;i++)a[i]=PathHash(p.GetInteriorRingN(i).Coordinates,true);return Word(Word(Mark('A'),PathHash(p.ExteriorRing.Coordinates,true),8),Components('H',a),8);}if(g is LineString)return PathHash(g.Coordinates,false);if(g is GeometryCollection){var a=new ulong[g.NumGeometries];for(int i=0;i<a.Length;i++)a[i]=GeometryHash(g.GetGeometryN(i));return Components(g is MultiPolygon?'M':'C',a);}throw new ArgumentException(g.GeometryType);}
 static object GeometryWork(IEnumerable<IFeature> q){long n=0;ulong sum=0,xor=0;foreach(var f in q){n++;ulong m=Mix(Typed(f)^Mix(GeometryHash(f.ToGeometry())));sum+=m;xor^=m;}return new{features=n,items=n,sum=sum.ToString(),xor=xor.ToString()};}

 static ulong Typed(IFeature f)=>(ulong)f.Id*4+(f.IsNode?0UL:f.IsWay?1UL:2UL);
 static long[] Consume(IEnumerable<IFeature> q,string op){long n=0,items=0;foreach(var f in q){n++;if(op=="iterate")items++;else if(op=="tags"){foreach(var t in f.Tags){Volatile.Write(ref sink,t.Key);Volatile.Write(ref sink,t.Value);items++;}}else if(op=="geometry"){Volatile.Write(ref sink,f.ToGeometry());items++;}else throw new ArgumentException(op);}return [n,items];}
 static object Fingerprint(IEnumerable<IFeature> q){ulong sum=0,xor=0;long count=0;foreach(var f in q){var m=Mix(Typed(f));sum+=m;xor^=m;count++;}return new{count,sum=sum.ToString(),xor=xor.ToString()};}
 static double Ns(long start)=>Stopwatch.GetElapsedTime(start).TotalNanoseconds;
 static void Main(string[] args){int samples=int.Parse(args[2]);double target=double.Parse(args[3])*1e6;long start=Stopwatch.GetTimestamp();using var library=FeatureLibrary.Open(args[0]);double open=Ns(start);
 foreach(var line in File.ReadLines(args[1])){var s=line.Split('\t');var name=s[0];var op=s[1];Console.Error.WriteLine("BEGIN "+name);try{
 start=Stopwatch.GetTimestamp();var query=library.Select(s[6]);if(s[2]!="")query=query.In(new Box(int.Parse(s[2]),int.Parse(s[3]),int.Parse(s[4]),int.Parse(s[5])));double setup=Ns(start);
 start=Stopwatch.GetTimestamp();var expected=Consume(query,op);double first=Ns(start);start=Stopwatch.GetTimestamp();do{Consume(query,op);}while(Ns(start)<100e6);
 start=Stopwatch.GetTimestamp();Consume(query,op);int repeats=(int)Math.Clamp(target/Math.Max(1,Ns(start)),1,1000);var wall=new double[samples];var cpu=new double[samples];var alloc=new long[samples];
 for(int i=0;i<samples;i++){long a=GC.GetTotalAllocatedBytes(true);double c=Process.GetCurrentProcess().TotalProcessorTime.TotalNanoseconds;start=Stopwatch.GetTimestamp();for(int r=0;r<repeats;r++){var got=Consume(query,op);if(got[0]!=expected[0]||got[1]!=expected[1])throw new Exception("unstable consumption");}wall[i]=Ns(start)/repeats;cpu[i]=(Process.GetCurrentProcess().TotalProcessorTime.TotalNanoseconds-c)/repeats;alloc[i]=(GC.GetTotalAllocatedBytes(true)-a)/repeats;}
 Console.WriteLine(JsonSerializer.Serialize(new{engine="dotnet",name,operation=op,count=expected[0],items=expected[1],open_ns=open,setup_ns=setup,first_ns=first,repeats,samples_wall_ns=wall,samples_cpu_ns=cpu,samples_allocated_bytes=alloc,fingerprint=Fingerprint(query),work_fingerprint=op=="geometry"?GeometryWork(query):null,validation_level="identities_and_consumption_counts",heap_used_bytes=GC.GetTotalMemory(false)}));
 }catch(Exception e){Console.WriteLine(JsonSerializer.Serialize(new{engine="dotnet",name,error=e.ToString()}));}}
 }
}
