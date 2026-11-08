#pragma once
// prbviz: per-slot debug prints (several per slot, ~2000 slots/s) serialize all worker threads on stdout and
// make nrscope drop slots once a UE is attached. They are off unless NRSCOPE_VERBOSE is set.
#include <cstdlib>
inline bool prbviz_verbose()
{
  static const bool v = std::getenv("NRSCOPE_VERBOSE") != nullptr;
  return v;
}
