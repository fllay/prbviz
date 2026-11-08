#pragma once
// prbviz: cheap opt-in stage timers. NRSCOPE_PROF=1 -> every 10 s one block per run is appended to ./prof.log:
// calls/s, wall ms per call, thread-CPU ms per call and cores (= CPU seconds per second) for each timed section.
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <mutex>
#include <thread>
#include <vector>

namespace prbviz_prof {
inline bool on()
{
  static const bool v = std::getenv("NRSCOPE_PROF") != nullptr;
  return v;
}
inline uint64_t now_ns(clockid_t c)
{
  timespec ts;
  clock_gettime(c, &ts);
  return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}
struct Acc
{
  const char*           name;
  std::atomic<uint64_t> n{0}, wall{0}, cpu{0};
  explicit Acc(const char* nm);
};
inline std::vector<Acc*>& reg()
{
  static std::vector<Acc*> v;
  return v;
}
inline std::mutex& regmx()
{
  static std::mutex m;
  return m;
}
inline void reporter()
{
  struct Snap { uint64_t n, wall, cpu; };
  std::vector<Snap> last;
  uint64_t          t_last = now_ns(CLOCK_MONOTONIC);
  FILE*             f      = fopen("prof.log", "a");
  if (!f) return;
  for (;;) {
    std::this_thread::sleep_for(std::chrono::seconds(10));
    uint64_t t = now_ns(CLOCK_MONOTONIC);
    double   dt = (double)(t - t_last) / 1e9;
    t_last = t;
    std::lock_guard<std::mutex> lk(regmx());
    if (last.size() < reg().size()) last.resize(reg().size(), Snap{0, 0, 0});
    time_t tt = time(nullptr);
    char   ts[32];
    strftime(ts, sizeof ts, "%H:%M:%S", localtime(&tt));
    fprintf(f, "---- %s (%.1f s)\n", ts, dt);
    for (size_t i = 0; i < reg().size(); i++) {
      Acc*     a  = reg()[i];
      uint64_t n = a->n.load(), w = a->wall.load(), c = a->cpu.load();
      double   dn = (double)(n - last[i].n), dw = (double)(w - last[i].wall), dc = (double)(c - last[i].cpu);
      last[i] = Snap{n, w, c};
      if (dn <= 0) continue;
      fprintf(f, "%-14s calls/s=%8.1f wall=%8.3f ms cpu=%8.3f ms cores=%6.2f\n", a->name, dn / dt, dw / dn / 1e6, dc / dn / 1e6, dc / 1e9 / dt);
    }
    fflush(f);
  }
}
inline Acc::Acc(const char* nm) : name(nm)
{
  if (!on()) return;
  static std::once_flag once;
  std::call_once(once, [] { std::thread(reporter).detach(); });
  std::lock_guard<std::mutex> lk(regmx());
  reg().push_back(this);
}
struct Scope
{
  Acc*     a;
  uint64_t w0 = 0, c0 = 0;
  explicit Scope(Acc* a_) : a(on() ? a_ : nullptr)
  {
    if (a) { w0 = now_ns(CLOCK_MONOTONIC); c0 = now_ns(CLOCK_THREAD_CPUTIME_ID); }
  }
  ~Scope()
  {
    if (a) {
      a->n.fetch_add(1, std::memory_order_relaxed);
      a->wall.fetch_add(now_ns(CLOCK_MONOTONIC) - w0, std::memory_order_relaxed);
      a->cpu.fetch_add(now_ns(CLOCK_THREAD_CPUTIME_ID) - c0, std::memory_order_relaxed);
    }
  }
};
}  // namespace prbviz_prof
#define PROF(var, nm) static prbviz_prof::Acc var##_acc(nm); prbviz_prof::Scope var(&var##_acc)
