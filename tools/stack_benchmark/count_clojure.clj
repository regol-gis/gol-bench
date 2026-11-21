(import '[com.geodesk.feature Features])
(import '[java.util.function BiFunction])
(set! *warn-on-reflection* true)
(CorpusJava/run (into-array String *command-line-args*)
  (reify BiFunction
    (apply [_ q _]
      (let [n (.count ^Features q)] (long-array [n n])))) "clojure-count")
