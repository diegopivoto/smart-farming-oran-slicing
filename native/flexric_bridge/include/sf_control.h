#ifndef SMARTFARM_SF_CONTROL_H
#define SMARTFARM_SF_CONTROL_H

#include <stdbool.h>
#include <stddef.h>

#define SF_PROFILE_PREFIX "sf=0.1;"
#define SF_MAX_LABEL_BYTES 511U

typedef struct {
  char *data;
  size_t cap;
  size_t len;
  bool truncated;
} sf_json_buffer_t;

/* Appends formatted text; marks the buffer truncated instead of overflowing. */
void sf_json_printf(sf_json_buffer_t *buf, const char *fmt, ...) __attribute__((format(printf, 2, 3)));

/* Appends a JSON string literal (quoted and escaped) from raw bytes. */
void sf_json_string(sf_json_buffer_t *buf, const char *data, size_t len);

/* True when the label belongs to the E2SM-SF/0.1 profile. */
bool sf_is_profile_label(const char *label, size_t len);

/* True when the label's second pair is "kind=<kind>". */
bool sf_label_has_kind(const char *label, size_t len, const char *kind);

/* Validates "CTRL <label>"; returns NULL and the label span, or an error text. */
const char *sf_parse_control_line(const char *line, size_t len, const char **label, size_t *label_len);

#endif
