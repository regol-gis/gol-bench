// Original benchmark adapter; uses only the public Java SDK API.
import com.geodesk.feature.*;
import com.geodesk.geom.Box;
import org.locationtech.jts.geom.*;
import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.lang.management.ManagementFactory;
import java.util.*;
import java.util.function.BiFunction;

public class CorpusJava {
    static volatile Object sink;
    public static void blackhole(Object value) { sink=value; }
    static long mix(long x) { x=(x^(x>>>30))*0xbf58476d1ce4e5b9L; x=(x^(x>>>27))*0x94d049bb133111ebL; return x^(x>>>31); }
    static long bytes(long h, byte[] data) { for(byte b:data) h=(h^(b&255))*1099511628211L; return h; }
    static long word(long h,long v,int n) { for(int i=0;i<n;i++){h=(h^(v&255))*1099511628211L;v>>>=8;}return h; }
    static final long FNV=0xcbf29ce484222325L;
    static long typed(Feature f){return f.id()*4+(f.isNode()?0:f.isWay()?1:2);}
    static long seq(char kind,Coordinate[] a,int start,boolean reverse){long h=word(bytes(FNV,new byte[]{(byte)kind}),a.length,8);for(int i=0;i<a.length;i++){int k=(start+(reverse?-i:i)+a.length)%a.length;h=word(word(h,(int)a[k].x,4),(int)a[k].y,4);}return h;}
    // Linear least cyclic rotation, matching the independent existing verifier.
    static int rotation(Coordinate[] a){int n=a.length,i=0,j=1,k=0;while(i<n&&j<n&&k<n){Coordinate x=a[(i+k)%n],y=a[(j+k)%n];int c=Double.compare(x.x,y.x);if(c==0)c=Double.compare(x.y,y.y);if(c==0){k++;continue;}if(c>0){i+=k+1;if(i<=j)i=j+1;}else{j+=k+1;if(j<=i)j=i+1;}k=0;}return Math.min(i,j);}
    static long path(Coordinate[] a,boolean ring){if(ring&&a.length>0&&a[0].equals2D(a[a.length-1]))a=Arrays.copyOf(a,a.length-1);long f=seq(ring?'R':'L',a,ring?rotation(a):0,false);Coordinate[] b=a.clone();for(int i=0;i<b.length;i++)b[i]=a[a.length-1-i];long r=seq(ring?'R':'L',b,ring?rotation(b):0,false);return Long.compareUnsigned(f,r)<0?f:r;}
    static long components(char kind,long[] a){Long[] b=new Long[a.length];for(int i=0;i<a.length;i++)b[i]=a[i];Arrays.sort(b,Long::compareUnsigned);long h=word(bytes(FNV,new byte[]{(byte)kind}),a.length,8);for(long v:b)h=word(h,v,8);return h;}
    static long geometry(Geometry g){if(g instanceof Point)return seq('P',g.getCoordinates(),0,false);if(g instanceof Polygon p){long[] holes=new long[p.getNumInteriorRing()];for(int i=0;i<holes.length;i++)holes[i]=path(p.getInteriorRingN(i).getCoordinates(),true);return word(word(bytes(FNV,new byte[]{'A'}),path(p.getExteriorRing().getCoordinates(),true),8),components('H',holes),8);}if(g instanceof LineString)return path(g.getCoordinates(),false);if(g instanceof GeometryCollection){long[] kids=new long[g.getNumGeometries()];for(int i=0;i<kids.length;i++)kids[i]=geometry(g.getGeometryN(i));return components(g instanceof MultiPolygon?'M':'C',kids);}throw new IllegalArgumentException(g.getGeometryType());}
    static long[] consume(Features q,String op,boolean verify){long n=0,items=0,sum=0,xor=0;for(Feature f:q){n++;long parent=verify?typed(f):0;if(op.equals("iterate")){items++;if(verify){long m=mix(parent);sum+=m;xor^=m;}}else if(op.equals("tags")){Tags t=f.tags();while(t.next()){String k=t.key();Object v=t.value();blackhole(k);sink=v;items++;if(verify){long h=bytes(bytes(FNV,k.getBytes(StandardCharsets.UTF_8)),new byte[]{(byte)255});h=v instanceof String?bytes(bytes(h,new byte[]{'T'}),((String)v).getBytes(StandardCharsets.UTF_8)):word(bytes(h,new byte[]{'N'}),Double.doubleToLongBits(((Number)v).doubleValue()),8);long m=mix(parent^mix(h));sum+=m;xor^=m;}}}else if(op.equals("geometry")){Geometry g=f.toGeometry();sink=g;items++;if(verify){long m=mix(parent^mix(geometry(g)));sum+=m;xor^=m;}}else throw new IllegalArgumentException(op);}return new long[]{n,items,sum,xor};}
    static String digest(long[] a){return "{\"count\":"+a[0]+",\"sum\":\""+Long.toUnsignedString(a[2])+"\",\"xor\":\""+Long.toUnsignedString(a[3])+"\"}";}
    static String work(long[] a){return "{\"features\":"+a[0]+",\"items\":"+a[1]+",\"sum\":\""+Long.toUnsignedString(a[2])+"\",\"xor\":\""+Long.toUnsignedString(a[3])+"\"}";}
    static long cpu(){return ((com.sun.management.OperatingSystemMXBean)ManagementFactory.getOperatingSystemMXBean()).getProcessCpuTime();}
    public static void run(String[] args,BiFunction<Features,String,long[]> consumer,String engine) throws Exception {
        int samples=Integer.parseInt(args[2]);long target=Long.parseLong(args[3])*1000000L;
        long open=System.nanoTime();try(var library=Features.open(args[0])){open=System.nanoTime()-open;
        for(String line:Files.readAllLines(Path.of(args[1]))){String[] s=line.split("\t",-1);String name=s[0],op=s[1];System.err.println("BEGIN "+name);
        try {long start=System.nanoTime();Features q=library.select(s[6]);if(!s[2].isEmpty())q=q.in(new Box(Integer.parseInt(s[2]),Integer.parseInt(s[3]),Integer.parseInt(s[4]),Integer.parseInt(s[5])));long setup=System.nanoTime()-start;
        start=System.nanoTime();long[] expected=consumer.apply(q,op);long first=System.nanoTime()-start;int repeats=1;
        if (samples == 0) {
            System.out.println("{\"name\":\"" + name + "\",\"count\":" + expected[0] +
                ",\"work_items\":" + expected[1] + ",\"open_ns\":" + open +
                ",\"setup_ns\":" + setup + ",\"first_execution_ns\":" + first + "}");
            continue;
        }
        // Warm compiled iteration and query matchers before recording steady state.
        long warmStart=System.nanoTime();do {consumer.apply(q,op);}while(System.nanoTime()-warmStart<100000000L);
        start=System.nanoTime();consumer.apply(q,op);long elapsed=Math.max(1,System.nanoTime()-start);repeats=(int)Math.max(1,Math.min(1000,target/elapsed));
        double[] wall=new double[samples],cpu=new double[samples];
        for(int i=0;i<samples;i++){long c=cpu();start=System.nanoTime();for(int j=0;j<repeats;j++){long[] got=consumer.apply(q,op);if(got[0]!=expected[0]||got[1]!=expected[1])throw new IllegalStateException("unstable consumption");}wall[i]=(System.nanoTime()-start)/(double)repeats;cpu[i]=(cpu()-c)/(double)repeats;}
        long[] ids=consume(q,"iterate",true);String wd=op.equals("iterate")?"null":work(consume(q,op,true));
        System.out.println("{\"engine\":\""+engine+"\",\"name\":\""+name+"\",\"operation\":\""+op+"\",\"count\":"+expected[0]+",\"items\":"+expected[1]+",\"open_ns\":"+open+",\"setup_ns\":"+setup+",\"first_ns\":"+first+",\"repeats\":"+repeats+",\"samples_wall_ns\":"+Arrays.toString(wall)+",\"samples_cpu_ns\":"+Arrays.toString(cpu)+",\"fingerprint\":"+digest(ids)+",\"work_fingerprint\":"+wd+",\"heap_used_bytes\":"+(Runtime.getRuntime().totalMemory()-Runtime.getRuntime().freeMemory())+"}");
        }catch(Exception e){String msg=e.toString().replace("\\","\\\\").replace("\"","\\\"").replace("\n"," ");System.out.println("{\"name\":\""+name+"\",\"engine\":\""+engine+"\",\"error\":\""+msg+"\"}");}}
        }
    }
    public static void main(String[] args)throws Exception{run(args,(q,op)->consume(q,op,false),"java");}
}
