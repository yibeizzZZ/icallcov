cmake_minimum_required(VERSION 3.14)

project(icall_trace C)

find_package(DynamoRIO REQUIRED)

add_library(icall_trace SHARED icall_trace.c)

configure_DynamoRIO_client(icall_trace)

use_DynamoRIO_extension(icall_trace drmgr)
