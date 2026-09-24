cmake_minimum_required(VERSION 3.14)
project(icall_ir_scan LANGUAGES C CXX)

# Optional, separate build: the DynamoRIO client does not depend on LLVM.
find_package(LLVM 21.1.0 REQUIRED CONFIG)
message(STATUS "Using LLVM ${LLVM_PACKAGE_VERSION} from ${LLVM_DIR}")

add_executable(icall_ir_scan icall_ir_scan.cpp)
target_compile_features(icall_ir_scan PRIVATE cxx_std_17)
target_include_directories(icall_ir_scan SYSTEM PRIVATE ${LLVM_INCLUDE_DIRS})
separate_arguments(LLVM_DEFINITION_FLAGS NATIVE_COMMAND "${LLVM_DEFINITIONS}")
target_compile_options(icall_ir_scan PRIVATE ${LLVM_DEFINITION_FLAGS})
# Distribution packages commonly expose the shared LLVM target. Prefer it
# rather than requiring development packages for every static LLVM dependency.
if(TARGET LLVM)
    target_link_libraries(icall_ir_scan PRIVATE LLVM)
else()
    llvm_map_components_to_libnames(LLVM_LIBS Core IRReader Support)
    target_link_libraries(icall_ir_scan PRIVATE ${LLVM_LIBS})
endif()
