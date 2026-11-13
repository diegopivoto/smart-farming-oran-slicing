#undef NDEBUG
#include "sf_control.h"

#include <assert.h>
#include <string.h>

static void test_control_line_validation(void)
{
  const char *label = NULL;
  size_t len = 0;
  const char good[] = "CTRL sf=0.1;op=prb_quota;action_id=1\r";
  assert(sf_parse_control_line(good, strlen(good), &label, &len) == NULL);
  assert(len == strlen("sf=0.1;op=prb_quota;action_id=1"));
  assert(memcmp(label, "sf=0.1;op=", 10) == 0);

  const char *bad[] = {
      "",
      "CTRL",
      "CTRL ",
      "ctrl sf=0.1;op=prb_quota",
      "CTRL sf=0.1;kind=outcome",
      "CTRL sf=0.2;op=prb_quota",
      "CTRL sf=0.1;op=prb_quota;x=1 2",
      "CTRL sf=0.1;op=prb_quota;\x01",
  };
  for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); i++)
    assert(sf_parse_control_line(bad[i], strlen(bad[i]), &label, &len) != NULL);

  char big[600] = "CTRL sf=0.1;op=";
  memset(big + strlen(big), 'a', sizeof(big) - strlen(big) - 1);
  big[sizeof(big) - 1] = '\0';
  assert(sf_parse_control_line(big, strlen(big), &label, &len) != NULL);
}

static void test_label_kind(void)
{
  const char outcome[] = "sf=0.1;kind=outcome;action_id=5";
  assert(sf_is_profile_label(outcome, strlen(outcome)));
  assert(sf_label_has_kind(outcome, strlen(outcome), "outcome"));
  assert(!sf_label_has_kind(outcome, strlen(outcome), "out"));
  assert(!sf_label_has_kind(outcome, strlen(outcome), "rule"));
  assert(!sf_is_profile_label("s1", 2));
  assert(!sf_is_profile_label(NULL, 0));
}

static void test_json_escaping_and_truncation(void)
{
  char data[64];
  sf_json_buffer_t buf = {.data = data, .cap = sizeof(data)};
  sf_json_string(&buf, "a\"b\\c\n", 6);
  assert(!buf.truncated);
  assert(strcmp(data, "\"a\\\"b\\\\c\\u000a\"") == 0);

  char tiny[4];
  sf_json_buffer_t small = {.data = tiny, .cap = sizeof(tiny)};
  sf_json_string(&small, "abcdef", 6);
  assert(small.truncated);
}

int main(void)
{
  test_control_line_validation();
  test_label_kind();
  test_json_escaping_and_truncation();
  return 0;
}
