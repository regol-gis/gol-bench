#include <geodesk/geodesk.h>

#include <cstdint>
#include <cstdio>
#include <exception>

extern "C" {
#include "php_ffi_api.h"
}

struct GolPhpLibrary {
    geodesk::Features features;
};

struct GolPhpQuery {
    geodesk::Features selection;
};

struct GolPhpIterator {
    explicit GolPhpIterator(const geodesk::Features& selection)
        : view(selection), current(view.begin()) {}

    geodesk::Features view;
    geodesk::FeatureIterator<geodesk::Feature> current;
};

namespace {
thread_local char last_error[512];

template<typename Function, typename Result>
auto checked(Function function, Result failure) -> decltype(function())
{
    try {
        return function();
    } catch (const std::exception& error) {
        std::snprintf(last_error, sizeof(last_error), "%s", error.what());
    } catch (...) {
        std::snprintf(last_error, sizeof(last_error), "Unknown GeoDesk C++ exception");
    }
    return failure;
}
}

const char* gol_php_error()
{
    return last_error;
}

GolPhpLibrary* gol_php_open(const char* path)
{
    return checked([&] { return new GolPhpLibrary{geodesk::Features(path)}; }, nullptr);
}

void gol_php_close(GolPhpLibrary* library)
{
    delete library;
}

GolPhpQuery* gol_php_prepare(GolPhpLibrary* library, const char* goql, const int32_t* bounds)
{
    return checked([&] {
        geodesk::Features selection = library->features(goql);
        if (bounds) {
            selection = selection(geodesk::Box(bounds[0], bounds[1], bounds[2], bounds[3]));
        }
        return new GolPhpQuery{selection};
    }, nullptr);
}

void gol_php_release_query(GolPhpQuery* query)
{
    delete query;
}

int64_t gol_php_count(const GolPhpQuery* query)
{
    return checked([&] { return static_cast<int64_t>(query->selection.count()); }, -1);
}

GolPhpIterator* gol_php_iterate(const GolPhpQuery* query)
{
    return checked([&] { return new GolPhpIterator(query->selection); }, nullptr);
}

void gol_php_release_iterator(GolPhpIterator* iterator)
{
    delete iterator;
}

int gol_php_next(GolPhpIterator* iterator, GolPhpFeature* output)
{
    return checked([&] {
        if (iterator->current == nullptr) {
            return 0;
        }
        geodesk::Feature feature = *iterator->current;
        output->id = static_cast<uint64_t>(feature.id());
        output->type = feature.isNode() ? 0 : feature.isWay() ? 1 : 2;
        ++iterator->current;
        return 1;
    }, -1);
}
