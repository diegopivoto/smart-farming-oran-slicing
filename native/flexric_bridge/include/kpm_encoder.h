#ifndef SMARTFARM_KPM_ENCODER_H
#define SMARTFARM_KPM_ENCODER_H

#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define SF_KPM_EPOCH_CAP 37U
#define SF_KPM_NODE_ID_CAP 96U
#define SF_KPM_UE_ID_CAP 96U
#define SF_KPM_NAME_CAP 128U
#define SF_KPM_UNIT_CAP 32U
#define SF_KPM_LABEL_NAME_CAP 32U
#define SF_KPM_LABEL_STRING_CAP 64U
#define SF_KPM_MAX_LABELS 16U
#define SF_KPM_MAX_MEASUREMENTS 64U
#define SF_KPM_QUEUE_CAPACITY 64U

typedef enum {
  SF_KPM_INTEGER = 0,
  SF_KPM_REAL = 1,
  SF_KPM_NO_VALUE = 2,
} sf_kpm_value_kind_t;

typedef enum {
  SF_KPM_LABEL_INTEGER = 0,
  SF_KPM_LABEL_REAL = 1,
  SF_KPM_LABEL_BOOLEAN = 2,
  SF_KPM_LABEL_STRING = 3,
} sf_kpm_label_kind_t;

typedef struct {
  char name[SF_KPM_LABEL_NAME_CAP];
  sf_kpm_label_kind_t kind;
  int64_t integer;
  double real;
  bool boolean;
  char string[SF_KPM_LABEL_STRING_CAP];
} sf_kpm_label_t;

typedef struct {
  char name[SF_KPM_NAME_CAP];
  sf_kpm_value_kind_t kind;
  int64_t integer;
  double real;
  bool has_unit;
  char unit[SF_KPM_UNIT_CAP];
  bool supported;
  size_t label_count;
  sf_kpm_label_t labels[SF_KPM_MAX_LABELS];
} sf_kpm_measurement_t;

typedef struct {
  char connection_epoch[SF_KPM_EPOCH_CAP];
  uint64_t sequence;
  uint64_t collected_at_ns;
  char e2_node_id[SF_KPM_NODE_ID_CAP];
  bool has_ue_id;
  char ue_id[SF_KPM_UE_ID_CAP];
  size_t measurement_count;
  sf_kpm_measurement_t measurements[SF_KPM_MAX_MEASUREMENTS];
} sf_kpm_sample_t;

typedef struct {
  atomic_bool gate;
  size_t head;
  size_t tail;
  atomic_size_t count;
  sf_kpm_sample_t slots[SF_KPM_QUEUE_CAPACITY];
} sf_kpm_queue_t;

typedef struct {
  atomic_uint_fast64_t received;
  atomic_uint_fast64_t enqueued;
  atomic_uint_fast64_t dropped;
  atomic_uint_fast64_t unknown;
} sf_kpm_counters_t;

bool sf_kpm_is_supported(const char *name);
bool sf_kpm_encode(const sf_kpm_sample_t *sample, char *out, size_t cap, size_t *written);

void sf_kpm_queue_init(sf_kpm_queue_t *queue);
bool sf_kpm_queue_push(sf_kpm_queue_t *queue, const sf_kpm_sample_t *sample);
bool sf_kpm_queue_pop(sf_kpm_queue_t *queue, sf_kpm_sample_t *sample);
size_t sf_kpm_queue_size(const sf_kpm_queue_t *queue);

void sf_kpm_counters_init(sf_kpm_counters_t *counters);
bool sf_kpm_callback_enqueue(
    sf_kpm_queue_t *queue,
    sf_kpm_counters_t *counters,
    const sf_kpm_sample_t *sample
);

#endif
