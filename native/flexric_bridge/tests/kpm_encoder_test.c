#include "kpm_encoder.h"

#include <assert.h>
#include <math.h>
#include <pthread.h>
#include <sched.h>
#include <stdio.h>
#include <string.h>

static sf_kpm_sample_t fake_sample(uint64_t sequence)
{
  sf_kpm_sample_t sample = {0};
  snprintf(sample.connection_epoch, sizeof(sample.connection_epoch),
           "11111111-1111-4111-8111-111111111111");
  sample.sequence = sequence;
  sample.collected_at_ns = 1793477000000000000ULL;
  snprintf(sample.e2_node_id, sizeof(sample.e2_node_id), "gnb:001-01:42");
  sample.has_ue_id = true;
  snprintf(sample.ue_id, sizeof(sample.ue_id), "gnb-amf-ngap:131");
  sample.measurement_count = 3;

  sf_kpm_measurement_t *throughput = &sample.measurements[0];
  snprintf(throughput->name, sizeof(throughput->name), "DRB.UEThpDl");
  throughput->kind = SF_KPM_REAL;
  throughput->real = 55.25;
  throughput->has_unit = true;
  snprintf(throughput->unit, sizeof(throughput->unit), "kbit/s");
  throughput->supported = true;
  throughput->label_count = 1;
  snprintf(throughput->labels[0].name, sizeof(throughput->labels[0].name), "slice");
  throughput->labels[0].kind = SF_KPM_LABEL_STRING;
  snprintf(throughput->labels[0].string, sizeof(throughput->labels[0].string),
           "video\"coffee");

  sf_kpm_measurement_t *prbs = &sample.measurements[1];
  snprintf(prbs->name, sizeof(prbs->name), "RRU.PrbTotDl");
  prbs->kind = SF_KPM_INTEGER;
  prbs->integer = 106;
  prbs->has_unit = true;
  snprintf(prbs->unit, sizeof(prbs->unit), "PRB");
  prbs->supported = true;
  prbs->label_count = 1;
  snprintf(prbs->labels[0].name, sizeof(prbs->labels[0].name), "incomplete");
  prbs->labels[0].kind = SF_KPM_LABEL_BOOLEAN;
  prbs->labels[0].boolean = false;

  sf_kpm_measurement_t *missing = &sample.measurements[2];
  snprintf(missing->name, sizeof(missing->name), "DRB.RlcSduDelayDl");
  missing->kind = SF_KPM_NO_VALUE;
  missing->supported = true;

  return sample;
}

static void test_encoder_preserves_values_and_explicit_absence(void)
{
  sf_kpm_sample_t sample = fake_sample(7);
  char json[8192] = {0};
  size_t written = 0;

  assert(sf_kpm_encode(&sample, json, sizeof(json), &written));
  assert(written == strlen(json));
  assert(json[written - 1] == '\n');
  assert(strstr(json, "\"schema_version\":1") != NULL);
  assert(strstr(json, "\"connection_epoch\":\"11111111-1111-4111-8111-111111111111\"") != NULL);
  assert(strstr(json, "\"ue_id\":\"gnb-amf-ngap:131\"") != NULL);
  assert(strstr(json, "\"name\":\"DRB.UEThpDl\"") != NULL);
  assert(strstr(json, "\"value\":55.25") != NULL);
  assert(strstr(json, "\"name\":\"RRU.PrbTotDl\"") != NULL);
  assert(strstr(json, "\"value\":106") != NULL);
  assert(strstr(json, "\"kind\":\"no_value\",\"value\":null") != NULL);
  assert(strstr(json, "\"slice\":\"video\\\"coffee\"") != NULL);
  assert(strstr(json, "\"incomplete\":false") != NULL);

  written = 999;
  assert(!sf_kpm_encode(&sample, json, 32, &written));
  assert(written == 0);
}

static void test_callback_only_enqueues_and_counts(void)
{
  static sf_kpm_queue_t queue;
  sf_kpm_counters_t counters;
  sf_kpm_queue_init(&queue);
  sf_kpm_counters_init(&counters);
  sf_kpm_sample_t input = fake_sample(9);
  sf_kpm_sample_t output = {0};

  assert(sf_kpm_callback_enqueue(&queue, &counters, &input));
  assert(sf_kpm_queue_size(&queue) == 1);
  assert(atomic_load(&counters.received) == 1);
  assert(atomic_load(&counters.enqueued) == 1);
  assert(atomic_load(&counters.dropped) == 0);
  assert(atomic_load(&counters.unknown) == 0);
  assert(sf_kpm_queue_pop(&queue, &output));
  assert(output.sequence == 9);
  assert(output.measurements[0].real == 55.25);
  assert(sf_kpm_queue_size(&queue) == 0);
  assert(!sf_kpm_queue_pop(&queue, &output));
}

static void test_full_queue_drops_without_waiting(void)
{
  static sf_kpm_queue_t queue;
  sf_kpm_counters_t counters;
  sf_kpm_queue_init(&queue);
  sf_kpm_counters_init(&counters);

  for (uint64_t i = 0; i < SF_KPM_QUEUE_CAPACITY; ++i) {
    sf_kpm_sample_t sample = fake_sample(i);
    assert(sf_kpm_callback_enqueue(&queue, &counters, &sample));
  }
  sf_kpm_sample_t overflow = fake_sample(1000);
  assert(!sf_kpm_callback_enqueue(&queue, &counters, &overflow));
  assert(sf_kpm_queue_size(&queue) == SF_KPM_QUEUE_CAPACITY);
  assert(atomic_load(&counters.received) == SF_KPM_QUEUE_CAPACITY + 1);
  assert(atomic_load(&counters.enqueued) == SF_KPM_QUEUE_CAPACITY);
  assert(atomic_load(&counters.dropped) == 1);
}

static void test_busy_queue_drops_without_spinning(void)
{
  static sf_kpm_queue_t queue;
  sf_kpm_counters_t counters;
  sf_kpm_queue_init(&queue);
  sf_kpm_counters_init(&counters);
  sf_kpm_sample_t sample = fake_sample(1001);

  assert(!atomic_exchange(&queue.gate, true));
  assert(!sf_kpm_callback_enqueue(&queue, &counters, &sample));
  atomic_store(&queue.gate, false);
  assert(atomic_load(&counters.received) == 1);
  assert(atomic_load(&counters.enqueued) == 0);
  assert(atomic_load(&counters.dropped) == 1);
}

static void test_consumer_can_pop_while_producer_gate_is_busy(void)
{
  static sf_kpm_queue_t queue;
  sf_kpm_counters_t counters;
  sf_kpm_queue_init(&queue);
  sf_kpm_counters_init(&counters);
  sf_kpm_sample_t input = fake_sample(1002);
  sf_kpm_sample_t output = {0};

  assert(sf_kpm_callback_enqueue(&queue, &counters, &input));
  assert(!atomic_exchange(&queue.gate, true));
  assert(sf_kpm_queue_pop(&queue, &output));
  assert(output.sequence == input.sequence);
  assert(atomic_load(&queue.gate));
  atomic_store(&queue.gate, false);
  assert(sf_kpm_queue_size(&queue) == 0U);
}

static void test_unknown_measurements_are_preserved_and_counted(void)
{
  static sf_kpm_queue_t queue;
  sf_kpm_counters_t counters;
  sf_kpm_queue_init(&queue);
  sf_kpm_counters_init(&counters);
  sf_kpm_sample_t sample = fake_sample(12);
  snprintf(sample.measurements[0].name, sizeof(sample.measurements[0].name),
           "VENDOR.Magic");
  sample.measurements[0].supported = false;

  assert(!sf_kpm_is_supported("VENDOR.Magic"));
  assert(sf_kpm_is_supported("DRB.UEThpDl"));
  assert(sf_kpm_callback_enqueue(&queue, &counters, &sample));
  assert(atomic_load(&counters.unknown) == 1);
}

static void test_encoder_rejects_non_json_numbers_and_contract_mismatch(void)
{
  sf_kpm_sample_t sample = fake_sample(13);
  char json[8192] = {0};
  size_t written = 0;

  sample.measurements[0].real = NAN;
  assert(!sf_kpm_encode(&sample, json, sizeof(json), &written));
  sample = fake_sample(13);
  sample.measurements[0].supported = false;
  assert(!sf_kpm_encode(&sample, json, sizeof(json), &written));
  sample = fake_sample(13);
  snprintf(sample.connection_epoch, sizeof(sample.connection_epoch), "not-a-uuid");
  assert(!sf_kpm_encode(&sample, json, sizeof(json), &written));
}

typedef struct {
  sf_kpm_queue_t *queue;
  sf_kpm_counters_t *counters;
  atomic_bool producer_done;
  atomic_uint_fast64_t consumed;
} concurrent_fixture_t;

static void *produce_samples(void *opaque)
{
  concurrent_fixture_t *fixture = opaque;
  for (uint64_t i = 0; i < 1000U; ++i) {
    sf_kpm_sample_t sample = fake_sample(i);
    (void)sf_kpm_callback_enqueue(fixture->queue, fixture->counters, &sample);
  }
  atomic_store(&fixture->producer_done, true);
  return NULL;
}

static void *consume_samples(void *opaque)
{
  concurrent_fixture_t *fixture = opaque;
  sf_kpm_sample_t sample = {0};
  while (!atomic_load(&fixture->producer_done) || sf_kpm_queue_size(fixture->queue) > 0U) {
    if (sf_kpm_queue_pop(fixture->queue, &sample)) {
      atomic_fetch_add(&fixture->consumed, 1U);
    } else {
      sched_yield();
    }
  }
  return NULL;
}

static void test_concurrent_producer_and_consumer_preserve_accounting(void)
{
  static sf_kpm_queue_t queue;
  sf_kpm_counters_t counters;
  sf_kpm_queue_init(&queue);
  sf_kpm_counters_init(&counters);
  concurrent_fixture_t fixture = {
      .queue = &queue,
      .counters = &counters,
  };
  atomic_init(&fixture.producer_done, false);
  atomic_init(&fixture.consumed, 0U);
  pthread_t producer;
  pthread_t consumer;

  assert(pthread_create(&consumer, NULL, consume_samples, &fixture) == 0);
  assert(pthread_create(&producer, NULL, produce_samples, &fixture) == 0);
  assert(pthread_join(producer, NULL) == 0);
  assert(pthread_join(consumer, NULL) == 0);

  const uint64_t received = atomic_load(&counters.received);
  const uint64_t enqueued = atomic_load(&counters.enqueued);
  const uint64_t dropped = atomic_load(&counters.dropped);
  assert(received == 1000U);
  assert(enqueued + dropped == received);
  assert(atomic_load(&fixture.consumed) == enqueued);
  assert(sf_kpm_queue_size(&queue) == 0U);
}

int main(int argc, char **argv)
{
  if (argc == 2 && strcmp(argv[1], "--emit-json") == 0) {
    sf_kpm_sample_t sample = fake_sample(7);
    char json[8192] = {0};
    size_t written = 0;
    assert(sf_kpm_encode(&sample, json, sizeof(json), &written));
    assert(fwrite(json, 1U, written, stdout) == written);
    return 0;
  }
  test_encoder_preserves_values_and_explicit_absence();
  test_callback_only_enqueues_and_counts();
  test_full_queue_drops_without_waiting();
  test_busy_queue_drops_without_spinning();
  test_consumer_can_pop_while_producer_gate_is_busy();
  test_unknown_measurements_are_preserved_and_counted();
  test_encoder_rejects_non_json_numbers_and_contract_mismatch();
  test_concurrent_producer_and_consumer_preserve_accounting();
  puts("kpm_encoder_test: PASS");
  return 0;
}
