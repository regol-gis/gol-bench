// Shared with PHP FFI::cdef. This file uses plain C declarations.
// Destroy iterators before their query, and queries before their library.
typedef struct GolPhpLibrary GolPhpLibrary;
typedef struct GolPhpQuery GolPhpQuery;
typedef struct GolPhpIterator GolPhpIterator;

typedef struct {
    uint64_t id;
    int32_t type;
} GolPhpFeature;

const char *gol_php_error(void);
GolPhpLibrary *gol_php_open(const char *path);
void gol_php_close(GolPhpLibrary *library);
GolPhpQuery *gol_php_prepare(GolPhpLibrary *library, const char *goql, const int32_t *bounds);
void gol_php_release_query(GolPhpQuery *query);
int64_t gol_php_count(const GolPhpQuery *query);
GolPhpIterator *gol_php_iterate(const GolPhpQuery *query);
void gol_php_release_iterator(GolPhpIterator *iterator);
int gol_php_next(GolPhpIterator *iterator, GolPhpFeature *feature);
