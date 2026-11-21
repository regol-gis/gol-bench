// Diagnostic for GeoDesk's Linux Python wheel, which sizes its native pool
// from libstdc++ hardware_concurrency(). Load only in the controlled worker.
#include <thread>

unsigned std::thread::hardware_concurrency() noexcept { return 1; }
