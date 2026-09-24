source_filename = "same_line.c"
target triple = "x86_64-unknown-linux-gnu"

declare void @direct()
declare i32 @__gxx_personality_v0(...)

define void @dispatch(ptr %fp) !dbg !5 {
entry:
  call void @direct(), !dbg !10
  call void %fp(), !dbg !10
  call void %fp(), !dbg !10
  call void asm sideeffect "", ""(), !dbg !10
  ret void, !dbg !10
}

define void @through_invoke(ptr %fp) personality ptr @__gxx_personality_v0 {
entry:
  invoke void %fp() to label %ok unwind label %eh
ok:
  ret void
eh:
  %ex = landingpad { ptr, i32 } cleanup
  resume { ptr, i32 } %ex
}

define void @tail_call(ptr %fp) {
entry:
  tail call void %fp()
  ret void
}

!llvm.dbg.cu = !{!0}
!llvm.module.flags = !{!3, !4}
!0 = distinct !DICompileUnit(language: DW_LANG_C11, file: !1, producer: "icallcov fixture", isOptimized: false, runtimeVersion: 0, emissionKind: FullDebug)
!1 = !DIFile(filename: "same_line.c", directory: "/fixture/src")
!2 = !DISubroutineType(types: !{null})
!3 = !{i32 2, !"Dwarf Version", i32 4}
!4 = !{i32 2, !"Debug Info Version", i32 3}
!5 = distinct !DISubprogram(name: "dispatch", scope: !1, file: !1, line: 1, type: !2, scopeLine: 1, spFlags: DISPFlagDefinition, unit: !0)
!10 = !DILocation(line: 7, column: 3, scope: !5)
