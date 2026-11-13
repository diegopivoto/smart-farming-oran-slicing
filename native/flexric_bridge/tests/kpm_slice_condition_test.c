#include "kpm_slice_condition.h"

#include <assert.h>
#include <stdint.h>

int main(void)
{
  static const uint32_t expected_sds[] = {0x000001U, 0x000002U, 0x000003U};

  assert(sf_kpm_project_slice_count() == 3U);
  for (size_t i = 0U; i < sf_kpm_project_slice_count(); ++i) {
    uint32_t sd = 0U;
    uint8_t encoded[4] = {0U};
    assert(sf_kpm_project_slice_sd(i, &sd));
    assert(sd == expected_sds[i]);
    assert(sf_kpm_encode_snssai(1U, sd, encoded));
    assert(encoded[0] == 1U);
    assert(encoded[1] == 0U);
    assert(encoded[2] == 0U);
    assert(encoded[3] == (uint8_t)(i + 1U));
  }

  uint32_t unused = 0U;
  uint8_t encoded[4] = {0U};
  assert(!sf_kpm_project_slice_sd(3U, &unused));
  assert(!sf_kpm_project_slice_sd(0U, NULL));
  assert(!sf_kpm_encode_snssai(1U, 0x01000000U, encoded));
  assert(!sf_kpm_encode_snssai(1U, 0x000001U, NULL));
  return 0;
}
