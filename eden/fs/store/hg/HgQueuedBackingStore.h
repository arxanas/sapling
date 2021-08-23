/*
 * Copyright (c) Facebook, Inc. and its affiliates.
 *
 * This software may be used and distributed according to the terms of the
 * GNU General Public License version 2.
 */

#pragma once

#include <folly/Range.h>
#include <folly/Synchronized.h>
#include <sys/types.h>
#include <atomic>
#include <memory>
#include <vector>

#include "eden/fs/model/Hash.h"
#include "eden/fs/store/BackingStore.h"
#include "eden/fs/store/ObjectFetchContext.h"
#include "eden/fs/store/hg/HgBackingStore.h"
#include "eden/fs/store/hg/HgImportRequestQueue.h"
#include "eden/fs/telemetry/RequestMetricsScope.h"
#include "eden/fs/telemetry/TraceBus.h"

namespace facebook::eden {

class BackingStoreLogger;
class ReloadableConfig;
class HgBackingStore;
class LocalStore;
class EdenStats;
class HgImportRequest;
class StructuredLogger;

constexpr uint8_t kNumberHgQueueWorker = 32;

struct HgImportTraceEvent : TraceEventBase {
  enum EventType : uint8_t {
    QUEUE,
    START,
    FINISH,
  };

  enum ResourceType : uint8_t {
    BLOB,
    TREE,
  };

  static HgImportTraceEvent queue(
      uint64_t unique,
      ResourceType resourceType,
      const HgProxyHash& proxyHash) {
    return HgImportTraceEvent{unique, QUEUE, resourceType, proxyHash};
  }

  static HgImportTraceEvent start(
      uint64_t unique,
      ResourceType resourceType,
      const HgProxyHash& proxyHash) {
    return HgImportTraceEvent{unique, START, resourceType, proxyHash};
  }

  static HgImportTraceEvent finish(
      uint64_t unique,
      ResourceType resourceType,
      const HgProxyHash& proxyHash) {
    return HgImportTraceEvent{unique, FINISH, resourceType, proxyHash};
  }

  HgImportTraceEvent(
      uint64_t unique,
      EventType eventType,
      ResourceType resourceType,
      const HgProxyHash& proxyHash);

  /// Simple accessor that hides the internal memory representation of paths.
  std::string getPath() const {
    return path.get();
  }

  // Unique per request, but is consistent across the three stages of an import:
  // queue, start, and finish. Used to correlate events to a request.
  uint64_t unique;
  EventType eventType;
  ResourceType resourceType;
  // The HG manifest node ID.
  Hash manifestNodeId;
  // Always null-terminated, and saves space in the trace event structure.
  std::unique_ptr<char[]> path;
};

/**
 * An Hg backing store implementation that will put incoming blob/tree import
 * requests into a job queue, then a pool of workers will work on fulfilling
 * these requests via different methods (reading from hgcache, Mononoke,
 * debugimporthelper, etc.).
 */
class HgQueuedBackingStore final : public BackingStore {
 public:
  HgQueuedBackingStore(
      std::shared_ptr<LocalStore> localStore,
      std::shared_ptr<EdenStats> stats,
      std::unique_ptr<HgBackingStore> backingStore,
      std::shared_ptr<ReloadableConfig> config,
      std::shared_ptr<StructuredLogger> structuredLogger,
      std::unique_ptr<BackingStoreLogger> logger,
      uint8_t numberThreads = kNumberHgQueueWorker);

  ~HgQueuedBackingStore() override;

  TraceBus<HgImportTraceEvent>& getTraceBus() const {
    return *traceBus_;
  }

  RootId parseRootId(folly::StringPiece rootId) override;
  std::string renderRootId(const RootId& rootId) override;

  folly::SemiFuture<std::unique_ptr<Tree>> getRootTree(
      const RootId& rootId,
      ObjectFetchContext& context) override;
  folly::SemiFuture<std::unique_ptr<TreeEntry>> getTreeEntryForRootId(
      const RootId& /* rootId */,
      TreeEntryType /* treeEntryType */,
      facebook::eden::PathComponentPiece /* pathComponentPiece */,
      ObjectFetchContext& /* context */) override {
    throw std::domain_error("unimplemented");
  }
  folly::SemiFuture<std::unique_ptr<Tree>> getTree(
      const Hash& id,
      ObjectFetchContext& context) override;
  folly::SemiFuture<std::unique_ptr<Blob>> getBlob(
      const Hash& id,
      ObjectFetchContext& context) override;

  FOLLY_NODISCARD virtual folly::SemiFuture<folly::Unit> prefetchBlobs(
      HashRange ids,
      ObjectFetchContext& context) override;

  /**
   * calculates `metric` for `object` imports that are `stage`.
   *    ex. HgQueuedBackingStore::getImportMetrics(
   *          RequestMetricsScope::HgImportStage::PENDING,
   *          RequestMetricsScope::HgImportObject::BLOB,
   *          RequestMetricsScope::Metric::COUNT,
   *        )
   *    calculates the number of blob imports that are pending
   */
  size_t getImportMetric(
      RequestMetricsScope::RequestStage stage,
      HgBackingStore::HgImportObject object,
      RequestMetricsScope::RequestMetric metric) const;

  void startRecordingFetch() override;
  void recordFetch(folly::StringPiece) override;
  std::unordered_set<std::string> stopRecordingFetch() override;

  folly::SemiFuture<folly::Unit> importManifestForRoot(
      const RootId& root,
      const Hash& manifest) override;

  HgBackingStore& getHgBackingStore() {
    return *backingStore_;
  }

  folly::StringPiece getRepoName() {
    return backingStore_->getRepoName();
  }

 private:
  // Forbidden copy constructor and assignment operator
  HgQueuedBackingStore(const HgQueuedBackingStore&) = delete;
  HgQueuedBackingStore& operator=(const HgQueuedBackingStore&) = delete;

  void processBlobImportRequests(
      std::vector<std::shared_ptr<HgImportRequest>>&& requests);
  void processTreeImportRequests(
      std::vector<std::shared_ptr<HgImportRequest>>&& requests);
  void processPrefetchRequests(
      std::vector<std::shared_ptr<HgImportRequest>>&& requests);

  /**
   * The worker runloop function.
   */
  void processRequest();

  void logMissingProxyHash();

  /**
   * Fetch a blob from Mercurial.
   *
   * For latency sensitive context, the caller is responsible for checking if
   * the blob is present locally, as this function will always push the request
   * at the end of the queue.
   */
  folly::SemiFuture<std::unique_ptr<Blob>> getBlobImpl(
      const Hash& id,
      const HgProxyHash& proxyHash,
      ObjectFetchContext& context);

  /**
   * Logs a backing store fetch to scuba if the path being fetched is
   * in the configured paths to log. If `identifer` is a RelativePathPiece this
   * will be used as the "path being fetched". If the `identifer` is a Hash
   * then this will look up the path with HgProxyHash to be used as the
   * "path being fetched"
   */
  void logBackingStoreFetch(
      ObjectFetchContext& context,
      const HgProxyHash& proxyHash,
      ObjectFetchContext::ObjectType type);

  /**
   * Similarly to logBackingStoreFetch, but on a batch of hashes.
   */
  void logBatchedBackingStoreFetch(
      ObjectFetchContext& context,
      const std::vector<HgProxyHash>& hashes,
      ObjectFetchContext::ObjectType type);

  /**
   * Internally used by the logBackingStoreFetch and
   * logBatchedBackingStoreFetch to log access to the given path.
   */
  void logFetch(
      ObjectFetchContext& context,
      RelativePathPiece path,
      ObjectFetchContext::ObjectType type,
      const std::optional<std::shared_ptr<RE2>>& logFetchPathRegex);

  /**
   * gets the watches timing `object` imports that are `stage`
   *    ex. HgQueuedBackingStore::getImportWatches(
   *          RequestMetricsScope::HgImportStage::PENDING,
   *          HgBackingStore::HgImportObject::BLOB,
   *        )
   *    gets the watches timing blob imports that are pending
   */
  RequestMetricsScope::LockedRequestWatchList& getImportWatches(
      RequestMetricsScope::RequestStage stage,
      HgBackingStore::HgImportObject object) const;

  /**
   * Gets the watches timing pending `object` imports
   *   ex. HgBackingStore::getPendingImportWatches(
   *          HgBackingStore::HgImportObject::BLOB,
   *        )
   *    gets the watches timing pending blob imports
   */
  RequestMetricsScope::LockedRequestWatchList& getPendingImportWatches(
      HgBackingStore::HgImportObject object) const;

  /**
   * isRecordingFetch_ indicates if HgQueuedBackingStore is recording paths
   * for fetched files. Initially we don't record paths. When
   * startRecordingFetch() is called, isRecordingFetch_ is set to true and
   * recordFetch() will record the input path. When stopRecordingFetch() is
   * called, isRecordingFetch_ is set to false and recordFetch() no longer
   * records the input path.
   */
  std::atomic<bool> isRecordingFetch_{false};
  folly::Synchronized<std::unordered_set<std::string>> fetchedFilePaths_;

  std::shared_ptr<LocalStore> localStore_;
  std::shared_ptr<EdenStats> stats_;

  /**
   * Reference to the eden config, may be a null pointer in unit tests.
   */
  std::shared_ptr<ReloadableConfig> config_;

  std::unique_ptr<HgBackingStore> backingStore_;

  /**
   * The import request queue. This queue is unbounded. This queue
   * implementation will ensure enqueue operation never blocks.
   */
  HgImportRequestQueue queue_;

  /**
   * The worker thread pool. These threads will be running `processRequest`
   * forever to process incoming import requests
   */
  std::vector<std::thread> threads_;

  std::shared_ptr<StructuredLogger> structuredLogger_;

  /**
   * Logger for backing store imports
   */
  std::unique_ptr<BackingStoreLogger> logger_;

  // The last time we logged a missing proxy hash so the minimum interval is
  // limited to EdenConfig::missingHgProxyHashLogInterval.
  folly::Synchronized<std::chrono::steady_clock::time_point>
      lastMissingProxyHashLog_;

  // Track metrics for queued imports
  mutable RequestMetricsScope::LockedRequestWatchList pendingImportBlobWatches_;
  mutable RequestMetricsScope::LockedRequestWatchList pendingImportTreeWatches_;
  mutable RequestMetricsScope::LockedRequestWatchList
      pendingImportPrefetchWatches_;

  // This field should be last so any internal subscribers can capture [this].
  std::shared_ptr<TraceBus<HgImportTraceEvent>> traceBus_;
};

} // namespace facebook::eden
