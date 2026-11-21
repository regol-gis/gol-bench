// Scalar aggregate workload; native count is intentionally allowed in this lane.
public class CorpusCountJava {
    public static void main(String[] args) throws Exception {
        CorpusJava.run(args, (query, operation) -> {
            long count = query.count();
            return new long[]{count, count};
        }, "java-count");
    }
}
