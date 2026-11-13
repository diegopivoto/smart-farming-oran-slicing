#ifndef SMARTFARM_KPM_SLICE_CONDITION_H
#define SMARTFARM_KPM_SLICE_CONDITION_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/*
 * OAI (ran_func_kpm.c, nssai_matches) treats a 1-byte S-NSSAI condition as
 * "SST with no SD" and only matches DRBs whose SD is 0xffffff. The lab UEs
 * always carry an SD, so each project slice needs its own 4-byte condition.
 */
size_t sf_kpm_project_slice_count(void);
bool sf_kpm_project_slice_sd(size_t index, uint32_t *sd);

/* Encodes SST (1 octet) followed by the 24-bit SD in network byte order. */
bool sf_kpm_encode_snssai(uint8_t sst, uint32_t sd, uint8_t encoded[4]);

#endif
