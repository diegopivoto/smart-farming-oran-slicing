cmake_minimum_required(VERSION 3.20)
project(smartfarm_un3gpp_e2_proxy VERSION 0.1.0 LANGUAGES C)

include(CTest)
find_package(Threads REQUIRED)

add_library(un3gpp_proxy_core STATIC src/un3gpp_proxy_core.c)
target_include_directories(un3gpp_proxy_core PUBLIC include)
target_compile_features(un3gpp_proxy_core PUBLIC c_std_11)
target_compile_options(un3gpp_proxy_core PRIVATE -Wall -Wextra -Werror -Wpedantic)

if(BUILD_TESTING)
  add_executable(un3gpp_proxy_core_test tests/un3gpp_proxy_core_test.c)
  target_link_libraries(un3gpp_proxy_core_test PRIVATE un3gpp_proxy_core)
  add_test(NAME un3gpp_proxy_core_test COMMAND un3gpp_proxy_core_test)
endif()

# The proxy itself needs the pinned FlexRIC agent SDK (built on PC2 with the RIC's options)
set(FLEXRIC_SOURCE_DIR "" CACHE PATH "Pinned FlexRIC source directory")
set(FLEXRIC_BUILD_DIR "" CACHE PATH "Matching FlexRIC build directory")
if(FLEXRIC_SOURCE_DIR AND FLEXRIC_BUILD_DIR)
  add_executable(smartfarm_un3gpp_e2_proxy
    src/e2_proxy.c
    ${FLEXRIC_SOURCE_DIR}/test/rnd/fill_rnd_data_e2_setup_req.c
    ${FLEXRIC_SOURCE_DIR}/src/util/time_now_us.c
  )
  target_compile_definitions(smartfarm_un3gpp_e2_proxy PRIVATE E2AP_V2 KPM_V2_03)
  target_include_directories(smartfarm_un3gpp_e2_proxy PRIVATE "${FLEXRIC_SOURCE_DIR}/src" "${FLEXRIC_SOURCE_DIR}")
  target_link_libraries(smartfarm_un3gpp_e2_proxy PRIVATE
    un3gpp_proxy_core
    "${FLEXRIC_BUILD_DIR}/src/agent/libe2_agent.a"
    Threads::Threads dl sctp m
  )
endif()
