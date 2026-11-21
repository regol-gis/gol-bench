import com.geodesk.feature.Feature;
import com.geodesk.feature.Features;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.TreeSet;

public class Replay {
    public static void main(String[] args) throws Exception {
        var sequence = Files.readAllLines(Path.of(args[1]));
        var counts = new long[sequence.size()];
        var elapsed = new long[sequence.size()];
        long start = System.nanoTime();
        try (var library = Features.open(args[0])) {
            var queries = new HashMap<String, Features>();
            for (String text : new TreeSet<>(sequence)) {
                queries.put(text, library.select(text));
            }
            for (int i = 0; i < sequence.size(); i++) {
                long count = 0;
                for (Feature feature : queries.get(sequence.get(i))) count++;
                counts[i] = count;
                elapsed[i] = System.nanoTime() - start;
            }
        }
        for (int i = 0; i < counts.length; i++) {
            System.out.println(counts[i] + " " + elapsed[i]);
        }
    }
}
