#pragma once

#include <memory>

namespace DB
{

/// Opaque RAII guard held alive for the duration of `IDisk::checkAccess`.
/// Subclasses use it to install thread-local state for the access check
/// (lower retry budgets, tighter timeouts, etc.) without leaking
/// storage-specific details into IDisk.
struct AccessCheckScope
{
    virtual ~AccessCheckScope() = default;
};

using AccessCheckScopePtr = std::unique_ptr<AccessCheckScope>;

}
