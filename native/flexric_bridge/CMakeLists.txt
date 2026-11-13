cmake_minimum_required(VERSION 3.20)

project(smartfarm_flexric_bridge VERSION 0.1.0 LANGUAGES C)

include(CTest)
find_package(Threads REQUIRED)

get_filename_component(SMARTFARM_REPOSITORY_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)

add_library(smartfarm_kpm_encoder STATIC
  src/kpm_encoder.c
)
target_include_directories(smartfarm_kpm_encoder PUBLIC include)
target_compile_features(smartfarm_kpm_encoder PUBLIC c_std_11)
target_compile_options(smartfarm_kpm_encoder PRIVATE
  -Wall -Wextra -Werror -Wpedantic
)
target_link_libraries(smartfarm_kpm_encoder PUBLIC Threads::Threads)

add_library(smartfarm_kpm_slice_condition STATIC
  src/kpm_slice_condition.c
)
target_include_directories(smartfarm_kpm_slice_condition PUBLIC include)
target_compile_features(smartfarm_kpm_slice_condition PUBLIC c_std_11)
target_compile_options(smartfarm_kpm_slice_condition PRIVATE
  -Wall -Wextra -Werror -Wpedantic
)

add_library(smartfarm_sf_control_util STATIC
  src/sf_control_util.c
)
target_include_directories(smartfarm_sf_control_util PUBLIC include)
target_compile_features(smartfarm_sf_control_util PUBLIC c_std_11)
target_compile_options(smartfarm_sf_control_util PRIVATE
  -Wall -Wextra -Werror -Wpedantic
)

option(BUILD_FLEXRIC_BRIDGE "Build the native FlexRIC KPM xApp bridge" OFF)
set(FLEXRIC_SOURCE_DIR "" CACHE PATH "Pinned FlexRIC source directory")
set(FLEXRIC_BUILD_DIR "" CACHE PATH "Matching pinned FlexRIC build directory")

if(BUILD_FLEXRIC_BRIDGE)
  if(NOT EXISTS "${FLEXRIC_SOURCE_DIR}/src/xApp/e42_xapp_api.h")
    message(FATAL_ERROR "FLEXRIC_SOURCE_DIR does not contain the FlexRIC xApp SDK")
  endif()
  set(FLEXRIC_XAPP_LIBRARY "${FLEXRIC_BUILD_DIR}/src/xApp/libe42_xapp.a")
  if(NOT EXISTS "${FLEXRIC_XAPP_LIBRARY}")
    message(FATAL_ERROR "FLEXRIC_BUILD_DIR does not contain libe42_xapp.a")
  endif()
  set(
    FLEXRIC_RRC_LIBRARY
    "${FLEXRIC_BUILD_DIR}/examples/xApp/c/monitor/RRC_MESSAGES/libasn1_nr_rrc.a"
  )
  if(NOT EXISTS "${FLEXRIC_RRC_LIBRARY}")
    message(FATAL_ERROR "FLEXRIC_BUILD_DIR does not contain libasn1_nr_rrc.a")
  endif()

  add_executable(smartfarm_kpm_bridge src/kpm_bridge.c)
  target_compile_features(smartfarm_kpm_bridge PRIVATE c_std_11)
  target_compile_definitions(smartfarm_kpm_bridge PRIVATE
    ASN
    E2AP_V2
    KPM_V2_03
    SQLITE3_XAPP
  )
  target_compile_options(smartfarm_kpm_bridge PRIVATE
    -Wall -Wextra -Werror -Wpedantic
  )
  target_include_directories(smartfarm_kpm_bridge PRIVATE
    "${FLEXRIC_SOURCE_DIR}/src"
    "${FLEXRIC_SOURCE_DIR}/src/sm/rc_sm/ie/asn"
    "${FLEXRIC_BUILD_DIR}/examples/xApp/c/monitor/RRC_MESSAGES"
  )
  target_link_libraries(smartfarm_kpm_bridge PRIVATE
    smartfarm_kpm_encoder
    smartfarm_kpm_slice_condition
    "${FLEXRIC_XAPP_LIBRARY}"
    "${FLEXRIC_RRC_LIBRARY}"
    Threads::Threads
    dl
    sctp
  )

  # Separate process from the read-only KPM bridge: E2SM-SF/0.1 control xApp
  add_executable(smartfarm_sf_control src/sf_control.c)
  target_compile_features(smartfarm_sf_control PRIVATE c_std_11)
  target_compile_definitions(smartfarm_sf_control PRIVATE ASN E2AP_V2 KPM_V2_03 SQLITE3_XAPP)
  target_compile_options(smartfarm_sf_control PRIVATE -Wall -Wextra -Werror)
  # keep SLICE readback out of the SDK's SQLite writer (50-byte label buffer), see sf_control.c
  target_link_options(smartfarm_sf_control PRIVATE -Wl,--wrap=write_db_xapp)
  # send the CONTROL only after its pending event exists (SDK ACK race, see sf_control.c)
  target_link_options(smartfarm_sf_control PRIVATE
    -Wl,--wrap=e2ap_send_bytes_xapp -Wl,--wrap=add_pending_event -Wl,--wrap=rm_pending_event_ev
  )
  target_include_directories(smartfarm_sf_control PRIVATE
    "${FLEXRIC_SOURCE_DIR}/src"
    "${FLEXRIC_SOURCE_DIR}/src/sm/rc_sm/ie/asn"
    "${FLEXRIC_BUILD_DIR}/examples/xApp/c/monitor/RRC_MESSAGES"
  )
  target_link_libraries(smartfarm_sf_control PRIVATE
    smartfarm_sf_control_util
    "${FLEXRIC_XAPP_LIBRARY}"
    "${FLEXRIC_RRC_LIBRARY}"
    Threads::Threads
    dl
    sctp
  )
endif()

if(BUILD_TESTING)
  if(EXISTS "${SMARTFARM_REPOSITORY_ROOT}/.venv/bin/python")
    set(Python3_EXECUTABLE "${SMARTFARM_REPOSITORY_ROOT}/.venv/bin/python")
  endif()
  find_package(Python3 REQUIRED COMPONENTS Interpreter)

  add_executable(kpm_encoder_test tests/kpm_encoder_test.c)
  target_compile_features(kpm_encoder_test PRIVATE c_std_11)
  target_compile_options(kpm_encoder_test PRIVATE
    -Wall -Wextra -Werror -Wpedantic
  )
  target_link_libraries(kpm_encoder_test PRIVATE smartfarm_kpm_encoder)
  add_test(NAME kpm_encoder_test COMMAND kpm_encoder_test)
  add_executable(kpm_slice_condition_test tests/kpm_slice_condition_test.c)
  target_compile_features(kpm_slice_condition_test PRIVATE c_std_11)
  target_compile_options(kpm_slice_condition_test PRIVATE
    -Wall -Wextra -Werror -Wpedantic
  )
  target_link_libraries(kpm_slice_condition_test PRIVATE smartfarm_kpm_slice_condition)
  add_test(NAME kpm_slice_condition_test COMMAND kpm_slice_condition_test)
  add_executable(sf_control_test tests/sf_control_test.c)
  target_compile_features(sf_control_test PRIVATE c_std_11)
  target_compile_options(sf_control_test PRIVATE -Wall -Wextra -Werror -Wpedantic)
  target_link_libraries(sf_control_test PRIVATE smartfarm_sf_control_util)
  add_test(NAME sf_control_test COMMAND sf_control_test)
  add_test(
    NAME kpm_encoder_schema_contract
    COMMAND
      "${Python3_EXECUTABLE}"
      "${CMAKE_CURRENT_SOURCE_DIR}/tests/validate_encoder_json.py"
      "$<TARGET_FILE:kpm_encoder_test>"
      "${SMARTFARM_REPOSITORY_ROOT}"
  )
endif()
