;; Original adapter: idiomatic typed Java interop, no reflection in hot calls.
(import '[com.geodesk.feature Features Feature Tags])
(import '[java.util.function BiFunction])
(set! *warn-on-reflection* true)
(defn consume [^Features query ^String operation]
  (let [counts (long-array 2)]
    (doseq [^Feature f query]
      (aset-long counts 0 (unchecked-inc (aget counts 0)))
      (case operation
        "iterate" (aset-long counts 1 (unchecked-inc (aget counts 1)))
        "tags" (let [^Tags tags (.tags f)]
                 (while (.next tags)
                   (CorpusJava/blackhole (.key tags))
                   (CorpusJava/blackhole (.value tags))
                   (aset-long counts 1 (unchecked-inc (aget counts 1)))))
        "geometry" (do (CorpusJava/blackhole (.toGeometry f))
                       (aset-long counts 1 (unchecked-inc (aget counts 1))))))
    counts))
(CorpusJava/run (into-array String *command-line-args*)
  (reify BiFunction (apply [_ q op] (consume q op))) "clojure")
