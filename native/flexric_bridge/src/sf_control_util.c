#include "sf_control.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

void sf_json_printf(sf_json_buffer_t *buf, const char *fmt, ...)
{
  if (buf->truncated)
    return;
  va_list ap;
  va_start(ap, fmt);
  const int n = vsnprintf(buf->data + buf->len, buf->cap - buf->len, fmt, ap);
  va_end(ap);
  if (n < 0 || (size_t)n >= buf->cap - buf->len) {
    buf->truncated = true;
    return;
  }
  buf->len += (size_t)n;
}

void sf_json_string(sf_json_buffer_t *buf, const char *data, size_t len)
{
  sf_json_printf(buf, "\"");
  for (size_t i = 0; i < len && !buf->truncated; i++) {
    const unsigned char c = (unsigned char)data[i];
    if (c == '"' || c == '\\')
      sf_json_printf(buf, "\\%c", c);
    else if (c < 0x20 || c >= 0x7f)
      sf_json_printf(buf, "\\u%04x", c);
    else
      sf_json_printf(buf, "%c", c);
  }
  sf_json_printf(buf, "\"");
}

bool sf_is_profile_label(const char *label, size_t len)
{
  const size_t prefix = sizeof(SF_PROFILE_PREFIX) - 1;
  return label != NULL && len > prefix && memcmp(label, SF_PROFILE_PREFIX, prefix) == 0;
}

bool sf_label_has_kind(const char *label, size_t len, const char *kind)
{
  if (!sf_is_profile_label(label, len))
    return false;
  char expected[64];
  const int n = snprintf(expected, sizeof(expected), "kind=%s", kind);
  if (n < 0 || (size_t)n >= sizeof(expected))
    return false;
  const size_t offset = sizeof(SF_PROFILE_PREFIX) - 1;
  const size_t rest = len - offset;
  if (rest < (size_t)n || memcmp(label + offset, expected, (size_t)n) != 0)
    return false;
  return rest == (size_t)n || label[offset + (size_t)n] == ';';
}

const char *sf_parse_control_line(const char *line, size_t len, const char **label, size_t *label_len)
{
  static const char command[] = "CTRL ";
  if (len > 0 && line[len - 1] == '\r')
    len--;
  if (len < sizeof(command) - 1 || memcmp(line, command, sizeof(command) - 1) != 0)
    return "expected 'CTRL <label>'";
  const char *l = line + sizeof(command) - 1;
  const size_t n = len - (sizeof(command) - 1);
  if (n == 0 || n > SF_MAX_LABEL_BYTES)
    return "label length out of range";
  for (size_t i = 0; i < n; i++)
    if (l[i] < 0x21 || l[i] > 0x7e)
      return "label must be printable ASCII without spaces";
  static const char op_prefix[] = SF_PROFILE_PREFIX "op=";
  if (n < sizeof(op_prefix) - 1 || memcmp(l, op_prefix, sizeof(op_prefix) - 1) != 0)
    return "label is not an E2SM-SF/0.1 request";
  *label = l;
  *label_len = n;
  return NULL;
}
