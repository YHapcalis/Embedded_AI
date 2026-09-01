# 新工程 CMakeLists 模板（ESP-IDF 工程根）
# 用法：复制到新项目根目录，修改 PROJECT_NAME；构建需先 export ESP-IDF 环境
cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(MY_NEW_PROJECT)
