/*
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package org.apache.beam.sdk.metrics;

import java.io.Serializable;
import java.util.concurrent.atomic.AtomicReference;
import org.apache.beam.sdk.annotations.Internal;
import org.apache.beam.sdk.options.ExperimentalOptions;
import org.apache.beam.sdk.options.PipelineOptions;
import org.checkerframework.checker.nullness.qual.Nullable;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * The <code>Metrics</code> is a utility class for producing various kinds of metrics for reporting
 * properties of an executing pipeline.
 *
 * <p>Metrics are created by calling one of the static methods in this class. Each metric is
 * associated with a namespace and a name. The namespace allows grouping related metrics together
 * based on the definition while also disambiguating common names based on where they are defined.
 *
 * <p>Reported metrics are implicitly scoped to the transform within the pipeline that reported
 * them. This allows reporting the same metric name in multiple places and identifying the value
 * each transform reported, as well as aggregating the metric across
 *
 * <p>It is runner-dependent whether Metrics are accessible during pipeline execution or only after
 * jobs have completed.
 *
 * <p>Example:
 *
 * <pre><code> class SomeDoFn extends{@literal DoFn<String, String>} {
 *   private Counter counter = Metrics.counter(SomeDoFn.class, "my-counter");
 *
 *  {@literal @}ProcessElement
 *   public void processElement(ProcessContext c) {
 *     counter.inc();
 *     Metrics.counter(SomeDoFn.class, "my-counter2").inc();
 *   }
 * }</code></pre>
 *
 * <p>See {@link MetricResults} (available from the {@code PipelineResults} interface) for an
 * example off how to query metrics.
 */
public class Metrics {
  private static final Logger LOG = LoggerFactory.getLogger(Metrics.class);

  private Metrics() {}

  static class MetricsFlag {
    private static final AtomicReference<@Nullable MetricsFlag> INSTANCE = new AtomicReference<>();
    final boolean counterDisabled;
    final boolean stringSetDisabled;
    final boolean boundedTrieDisabled;
    final boolean lineageRollupEnabled;

    private MetricsFlag(
        boolean counterDisabled,
        boolean stringSetDisabled,
        boolean boundedTrieDisabled,
        boolean lineageRollupEnabled) {
      this.counterDisabled = counterDisabled;
      this.stringSetDisabled = stringSetDisabled;
      this.boundedTrieDisabled = boundedTrieDisabled;
      this.lineageRollupEnabled = lineageRollupEnabled;
    }

    static boolean counterDisabled() {
      MetricsFlag flag = INSTANCE.get();
      return flag != null && flag.counterDisabled;
    }

    static boolean stringSetDisabled() {
      MetricsFlag flag = INSTANCE.get();
      return flag != null && flag.stringSetDisabled;
    }

    static boolean boundedTrieDisabled() {
      MetricsFlag flag = INSTANCE.get();
      return flag != null && flag.boundedTrieDisabled;
    }

    static boolean lineageRollupEnabled() {
      MetricsFlag flag = INSTANCE.get();
      return flag != null && flag.lineageRollupEnabled;
    }
  }

  /**
   * Initialize metrics flags if not already done so.
   *
   * <p>Should be called by worker at worker harness initialization. Should not be called by user
   * code (and it does not have an effect as the initialization completed before).
   */
  @Internal
  public static void setDefaultPipelineOptions(PipelineOptions options) {
    MetricsFlag flag = MetricsFlag.INSTANCE.get();
    if (flag == null) {
      ExperimentalOptions exp = options.as(ExperimentalOptions.class);
      boolean counterDisabled = ExperimentalOptions.hasExperiment(exp, "disableCounterMetrics");
      if (counterDisabled) {
        LOG.info("Counter metrics are disabled.");
      }
      boolean stringSetDisabled = ExperimentalOptions.hasExperiment(exp, "disableStringSetMetrics");
      if (stringSetDisabled) {
        LOG.info("StringSet metrics are disabled");
      }
      boolean boundedTrieDisabled =
          ExperimentalOptions.hasExperiment(exp, "disableBoundedTrieMetrics");
      if (boundedTrieDisabled) {
        LOG.info("BoundedTrie metrics are disabled");
      }
      boolean lineageRollupEnabled = ExperimentalOptions.hasExperiment(exp, "enableLineageRollup");
      if (lineageRollupEnabled) {
        LOG.info("Lineage Rollup is enabled. Will use BoundedTrie to track lineage.");
      }
      MetricsFlag.INSTANCE.compareAndSet(
          null,
          new MetricsFlag(
              counterDisabled, stringSetDisabled, boundedTrieDisabled, lineageRollupEnabled));
    }
  }

  @Internal
  static void resetDefaultPipelineOptions() {
    MetricsFlag.INSTANCE.set(null);
  }

  /**
   * Create a metric that can be incremented and decremented, and is aggregated by taking the sum.
   */
  public static Counter counter(String namespace, String name) {
    return new DelegatingCounter(MetricName.named(namespace, name));
  }

  /**
   * Create a metric that can be incremented and decremented, and is aggregated by taking the sum.
   */
  public static Counter counter(Class<?> namespace, String name) {
    return new DelegatingCounter(MetricName.named(namespace, name));
  }

  /** Create a metric that records various statistics about the distribution of reported values. */
  public static Distribution distribution(String namespace, String name) {
    return new DelegatingDistribution(MetricName.named(namespace, name));
  }

  /** Create a metric that records various statistics about the distribution of reported values. */
  public static Distribution distribution(Class<?> namespace, String name) {
    return new DelegatingDistribution(MetricName.named(namespace, name));
  }

  /**
   * Create a metric that can have its new value set, and is aggregated by taking the last reported
   * value.
   */
  public static Gauge gauge(String namespace, String name) {
    return new DelegatingGauge(MetricName.named(namespace, name));
  }

  /**
   * Create a metric that can have its new value set, and is aggregated by taking the last reported
   * value.
   */
  public static Gauge gauge(Class<?> namespace, String name) {
    return new DelegatingGauge(MetricName.named(namespace, name));
  }

  /**
   * Create a metric that can have its new value set, and is aggregated by taking the last reported
   * value.
   */
  public static Gauge gauge(MetricName metricName) {
    return new DelegatingGauge(metricName);
  }

  /** Create a metric that accumulates and reports set of unique string values. */
  public static StringSet stringSet(String namespace, String name) {
    return new DelegatingStringSet(MetricName.named(namespace, name));
  }

  /** Create a metric that accumulates and reports set of unique string values. */
  public static StringSet stringSet(Class<?> namespace, String name) {
    return new DelegatingStringSet(MetricName.named(namespace, name));
  }

  /**
   * Create a metric that accumulates and reports set of unique string values bounded to a max
   * limit.
   */
  public static BoundedTrie boundedTrie(Class<?> namespace, String name) {
    return new DelegatingBoundedTrie(MetricName.named(namespace, name));
  }

  /**
   * Create a metric that accumulates and reports set of unique string values bounded to a max
   * limit.
   */
  public static BoundedTrie boundedTrie(String namespace, String name) {
    return new DelegatingBoundedTrie(MetricName.named(namespace, name));
  }

  /*
   * A dedicated namespace for client throttling time. User DoFn can increment this metrics and then
   * runner will put back pressure on scaling decision, if supported.
   */
  public static final String THROTTLE_TIME_NAMESPACE = "beam-throttling-metrics";
  public static final String THROTTLE_TIME_COUNTER_NAME = "throttling-msecs";

  /**
   * Implementation of {@link Distribution} that delegates to the instance for the current context.
   */
  private static class DelegatingDistribution implements Metric, Distribution, Serializable {
    private final MetricName name;

    private DelegatingDistribution(MetricName name) {
      this.name = name;
    }

    @Override
    public void update(long value) {
      MetricsContainer container = MetricsEnvironment.getCurrentContainer();
      if (container != null) {
        container.getDistribution(name).update(value);
      }
    }

    @Override
    public void update(long sum, long count, long min, long max) {
      MetricsContainer container = MetricsEnvironment.getCurrentContainer();
      if (container != null) {
        container.getDistribution(name).update(sum, count, min, max);
      }
    }

    @Override
    public MetricName getName() {
      return name;
    }
  }

  /** Implementation of {@link Gauge} that delegates to the instance for the current context. */
  private static class DelegatingGauge implements Metric, Gauge, Serializable {
    private final MetricName name;

    private DelegatingGauge(MetricName name) {
      this.name = name;
    }

    @Override
    public void set(long value) {
      MetricsContainer container = MetricsEnvironment.getCurrentContainer();
      if (container != null) {
        container.getGauge(name).set(value);
      }
    }

    @Override
    public MetricName getName() {
      return name;
    }
  }

  /** Implementation of {@link StringSet} that delegates to the instance for the current context. */
  private static class DelegatingStringSet implements Metric, StringSet, Serializable {
    private final MetricName name;

    private DelegatingStringSet(MetricName name) {
      this.name = name;
    }

    @Override
    public void add(String value) {
      if (MetricsFlag.stringSetDisabled()) {
        return;
      }
      MetricsContainer container = MetricsEnvironment.getCurrentContainer();
      if (container != null) {
        container.getStringSet(name).add(value);
      }
    }

    @Override
    public void add(String... value) {
      MetricsContainer container = MetricsEnvironment.getCurrentContainer();
      if (container != null) {
        container.getStringSet(name).add(value);
      }
    }

    @Override
    public MetricName getName() {
      return name;
    }
  }

  /**
   * Implementation of {@link BoundedTrie} that delegates to the instance for the current context.
   */
  private static class DelegatingBoundedTrie implements Metric, BoundedTrie, Serializable {
    private final MetricName name;

    private DelegatingBoundedTrie(MetricName name) {
      this.name = name;
    }

    @Override
    public MetricName getName() {
      return name;
    }

    @Override
    public void add(Iterable<String> values) {
      // If BoundedTrie metrics are disabled explicitly then do not emit them.
      if (MetricsFlag.boundedTrieDisabled()) {
        return;
      }
      MetricsContainer container = MetricsEnvironment.getCurrentContainer();
      if (container != null) {
        container.getBoundedTrie(name).add(values);
      }
    }
  }
}
