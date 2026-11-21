// Persistent service replay using GeoDesk's public C++ iteration API.
#include <geodesk/geodesk.h>
#include <chrono>
#include <fstream>
#include <iostream>
#include <map>
#include <set>
#include <string>
#include <vector>

int main(int argc, char** argv) {
    if (argc != 3) return 2;
    std::ifstream input(argv[2]);
    if (!input) return 2;
    std::vector<std::string> requests;
    for (std::string line; std::getline(input, line);) requests.push_back(line);
    using Clock = std::chrono::steady_clock;
    auto start = Clock::now();
    geodesk::Features library(argv[1]);
    std::map<std::string, geodesk::Features> plans;
    for (const auto& query : std::set<std::string>(requests.begin(), requests.end())) {
        plans.emplace(query, library(query.c_str()));
    }
    std::vector<std::pair<uint64_t, int64_t>> results;
    results.reserve(requests.size());
    for (const auto& query : requests) {
        uint64_t count = 0;
        for (geodesk::Feature feature : plans.at(query)) {
            asm volatile("" : : "g"(&feature) : "memory");
            ++count;
        }
        auto elapsed = std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now() - start);
        results.emplace_back(count, elapsed.count());
    }
    for (auto [count, ns] : results) std::cout << count << ' ' << ns << '\n';
}
