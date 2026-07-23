
# --- supernova variant (appended by the Wildrider supernova build) ------------
# mi-UGens' own CMake only builds the scsynth plugin (raw add_library, no
# SUPERNOVA path). supernova loads ONLY *_supernova.so, so clone the scsynth
# target generically (its sources + include dirs) into a second target compiled
# with SUPERNOVA defined — exactly what SC's sc_add_server_plugin macro does.
get_target_property(_wr_srcs ${PROJECT_NAME} SOURCES)
get_target_property(_wr_incs ${PROJECT_NAME} INCLUDE_DIRECTORIES)
add_library(${PROJECT_NAME}_supernova MODULE ${_wr_srcs})
target_include_directories(${PROJECT_NAME}_supernova PUBLIC ${_wr_incs} ${SC_PATH}/common)
set_target_properties(${PROJECT_NAME}_supernova PROPERTIES PREFIX "" CXX_VISIBILITY_PRESET hidden)
target_compile_definitions(${PROJECT_NAME}_supernova PUBLIC TEST)
target_compile_definitions(${PROJECT_NAME}_supernova PRIVATE SUPERNOVA)
