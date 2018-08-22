/*
 *  Copyright (c) 2004-present, Facebook, Inc.
 *  All rights reserved.
 *
 *  This source code is licensed under the BSD-style license found in the
 *  LICENSE file in the root directory of this source tree. An additional grant
 *  of patent rights can be found in the PATENTS file in the same directory.
 *
 */
#include <gtest/gtest.h>
#include <chrono>
#include "eden/fs/testharness/FakeFuse.h"
#include "eden/fs/testharness/FakeTreeBuilder.h"
#include "eden/fs/testharness/TestMount.h"
#include "eden/fs/utils/UnboundedQueueExecutor.h"

#include <folly/executors/ManualExecutor.h>
#include <folly/io/async/EventBase.h>
#include <folly/io/async/ScopedEventBaseThread.h>
#include <folly/logging/xlog.h>
#include <folly/test/TestUtils.h>

using namespace facebook::eden;
using namespace std::chrono_literals;
using folly::Future;
using folly::ScopedEventBaseThread;
using folly::Unit;
using std::make_shared;

namespace {

/**
 * The FUSE tests wait for work to finish on a thread pool. 250ms is too short
 * for the test to reliably test under heavy system load (such as when stress
 * testing), so wait for 10 seconds.
 */
constexpr std::chrono::seconds kWaitTimeout = 10s;

} // namespace

TEST(FuseTest, initMount) {
  auto builder1 = FakeTreeBuilder();
  builder1.setFile("src/main.c", "int main() { return 0; }\n");
  builder1.setFile("src/test/test.c", "testy tests");
  TestMount testMount{builder1};

  auto fuse = make_shared<FakeFuse>();
  testMount.registerFakeFuse(fuse);

  auto initFuture =
      testMount.getEdenMount()
          ->startFuse()
          .thenValue([](auto&&) { XLOG(INFO) << "startFuse() succeeded"; })
          .onError([&](const folly::exception_wrapper& ew) {
            ADD_FAILURE() << "startFuse() failed: " << folly::exceptionStr(ew);
          });

  struct fuse_init_in initArg;
  initArg.major = FUSE_KERNEL_VERSION;
  initArg.minor = FUSE_KERNEL_MINOR_VERSION;
  initArg.max_readahead = 0;
  initArg.flags = 0;
  auto reqID = fuse->sendRequest(FUSE_INIT, 1, initArg);
  auto response = fuse->recvResponse();
  EXPECT_EQ(reqID, response.header.unique);
  EXPECT_EQ(0, response.header.error);
  EXPECT_EQ(
      sizeof(fuse_out_header) + sizeof(fuse_init_out), response.header.len);

  // Wait for the mount to complete
  testMount.drainServerExecutor();
  std::move(initFuture).get(kWaitTimeout);

  // Close the FakeFuse device, and ensure that the mount's FUSE completion
  // future is then signalled.
  fuse->close();

  auto fuseCompletionFuture =
      testMount.getEdenMount()->getFuseCompletionFuture();

  // TestMount has a manual executor, but the fuse channel thread enqueues
  // the work. Wait for the future to complete, driving the ManualExecutor
  // all the while.
  // TODO: It might be worth moving this logic into a TestMount::waitFuture
  // method.
  auto deadline = std::chrono::steady_clock::now() + kWaitTimeout;
  do {
    if (std::chrono::steady_clock::now() > deadline) {
      FAIL() << "fuse completion future not ready within timeout";
    }
    testMount.drainServerExecutor();
  } while (!fuseCompletionFuture.isReady());

  auto mountInfo = std::move(fuseCompletionFuture.value());

  // Since we closed the FUSE device from the "kernel" side the returned
  // MountInfo should not contain a valid FUSE device any more.
  EXPECT_FALSE(mountInfo.fuseFD);
}

// Test destroying the EdenMount object while FUSE initialization is still
// pending
TEST(FuseTest, destroyBeforeInitComplete) {
  auto builder1 = FakeTreeBuilder();
  builder1.setFile("src/main.c", "int main() { return 0; }\n");
  builder1.setFile("src/test/test.c", "testy tests");

  auto fuse = make_shared<FakeFuse>();
  auto initFuture = Future<Unit>::makeEmpty();
  {
    // Create the TestMountj
    TestMount testMount{builder1};
    testMount.registerFakeFuse(fuse);

    // Call startFuse() on the test mount.
    initFuture = testMount.getEdenMount()->startFuse();

    // Exit the scope to destroy the mount
  }

  // The initFuture() should have completed unsuccessfully.
  EXPECT_THROW_RE(
      std::move(initFuture).get(100ms),
      std::runtime_error,
      "FuseChannel for .* stopped while waiting for INIT packet");
}

// Test destroying the EdenMount object immediately after the FUSE INIT request
// has been received.  We previously had some race conditions that could cause
// problems here.
TEST(FuseTest, destroyWithInitRace) {
  auto builder1 = FakeTreeBuilder();
  builder1.setFile("src/main.c", "int main() { return 0; }\n");
  builder1.setFile("src/test/test.c", "testy tests");

  auto fuse = make_shared<FakeFuse>();
  auto initFuture = Future<Unit>::makeEmpty();
  auto completionFuture = Future<TakeoverData::MountInfo>::makeEmpty();
  {
    // Create the TestMount.
    TestMount testMount{builder1};
    testMount.registerFakeFuse(fuse);

    // Call startFuse() on the test mount.
    initFuture = testMount.getEdenMount()->startFuse();
    completionFuture = testMount.getEdenMount()->getFuseCompletionFuture();

    // Send the FUSE INIT request.
    struct fuse_init_in initArg;
    initArg.major = FUSE_KERNEL_VERSION;
    initArg.minor = FUSE_KERNEL_MINOR_VERSION;
    initArg.max_readahead = 0;
    initArg.flags = 0;
    auto reqID = fuse->sendRequest(FUSE_INIT, 1, initArg);

    // Wait to receive the INIT reply from the FuseChannel code to confirm
    // that it saw the INIT request.
    auto response = fuse->recvResponse();
    EXPECT_EQ(reqID, response.header.unique);
    EXPECT_EQ(0, response.header.error);
    EXPECT_EQ(
        sizeof(fuse_out_header) + sizeof(fuse_init_out), response.header.len);

    // Exit the scope to destroy the TestMount.
    // This will call EdenMount::destroy() to start destroying the EdenMount.
    // However, this may not complete immediately.  Previously we had a bug
    // where the ServerState object was not guaranteed to survive until the
    // EdenMount was completely destroyed in this case.
  }

  // The initFuture should complete successfully, since we know that
  // FuseChannel sent the INIT response.
  std::move(initFuture).get(250ms);
  // The FUSE completion future should also be signalled when the FuseChannel
  // is destroyed.
  auto mountInfo = std::move(completionFuture).get(250ms);
  // Since we just destroyed the EdenMount and the kernel-side of the FUSE
  // channel was not stopped the returned MountInfo should contain the FUSE
  // device.
  EXPECT_TRUE(mountInfo.fuseFD);
}
