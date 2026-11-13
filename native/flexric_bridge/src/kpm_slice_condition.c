#include "kpm_slice_condition.h"

/* Smart Farming slices: weather station, crop inspection, drone video. */
static const uint32_t project_slice_sds[] = {0x000001U, 0x000002U, 0x000003U};

size_t sf_kpm_project_slice_count(void)
{
  return sizeof(project_slice_sds) / sizeof(project_slice_sds[0]);
}

bool sf_kpm_project_slice_sd(size_t index, uint32_t *sd)
{
  if (sd == NULL || index >= sf_kpm_project_slice_count()) {
    return false;
  }
  *sd = project_slice_sds[index];
  return true;
}

bool sf_kpm_encode_snssai(uint8_t sst, uint32_t sd, uint8_t encoded[4])
{
  if (encoded == NULL || sd > 0xffffffU) {
    return false;
  }
  encoded[0] = sst;
  encoded[1] = (uint8_t)((sd >> 16) & 0xffU);
  encoded[2] = (uint8_t)((sd >> 8) & 0xffU);
  encoded[3] = (uint8_t)(sd & 0xffU);
  return true;
}
