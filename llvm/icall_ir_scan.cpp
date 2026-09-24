#include "llvm/ADT/SmallString.h"
#include "llvm/ADT/StringExtras.h"
#include "llvm/Config/llvm-config.h"
#include "llvm/IR/DebugInfoMetadata.h"
#include "llvm/IR/Function.h"
#include "llvm/IR/InstrTypes.h"
#include "llvm/IR/LLVMContext.h"
#include "llvm/IR/Module.h"
#include "llvm/IR/Verifier.h"
#include "llvm/IRReader/IRReader.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/FormatVariadic.h"
#include "llvm/Support/InitLLVM.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/SHA256.h"
#include "llvm/Support/SourceMgr.h"
#include "llvm/Support/raw_ostream.h"

#include <algorithm>
#include <cstdint>
#include <memory>
#include <set>
#include <string>
#include <vector>

using namespace llvm;

static cl::OptionCategory Category("icallcov LLVM IR scanner options");
static cl::list<std::string> Inputs(cl::Positional, cl::OneOrMore,
                                    cl::desc("<input.ll|input.bc> ..."), cl::cat(Category));
static cl::opt<std::string> Output("o", cl::init("-"), cl::value_desc("path"),
                                   cl::desc("JSON output file (default: stdout)"),
                                   cl::cat(Category));

struct InputModule {
    std::string Path;
    std::string Digest;
    // Module must be destroyed before its owning context.
    std::unique_ptr<LLVMContext> Context;
    std::unique_ptr<Module> IR;
};

static std::string fingerprint(StringRef Data)
{
    SHA256 Hash;
    Hash.update(Data);
    return toHex(Hash.final(), true);
}

static json::Object sourcePosition(const DILocation *Location)
{
    return json::Object{{"file", Location->getFilename()},
            {"directory", Location->getDirectory()},
            {"line", static_cast<int64_t>(Location->getLine())},
            {"column", static_cast<int64_t>(Location->getColumn())},
            {"discriminator", static_cast<int64_t>(Location->getDiscriminator())}};
}

static json::Value sourceLocation(const Instruction &Instruction)
{
    const DebugLoc &Debug = Instruction.getDebugLoc();
    if (!Debug)
        return nullptr;
    json::Object Location = sourcePosition(Debug.get());
    json::Array InlineChain;
    for (const DILocation *At = Debug->getInlinedAt(); At != nullptr; At = At->getInlinedAt())
        InlineChain.push_back(sourcePosition(At));
    Location["inlined_at"] = std::move(InlineChain);
    return Location;
}

static void scanModule(const InputModule &Input, json::Array &Sites)
{
    unsigned FunctionIndex = 0;
    for (const Function &Function : *Input.IR) {
        const std::string FunctionID = Input.Digest + ":f" + std::to_string(FunctionIndex++);
        unsigned BlockIndex = 0;
        for (const BasicBlock &Block : Function) {
            unsigned InstructionIndex = 0;
            for (const Instruction &Instruction : Block) {
                const unsigned Index = InstructionIndex++;
                const auto *Call = dyn_cast<CallBase>(&Instruction);
                if (Call == nullptr || !Call->isIndirectCall())
                    continue;

                // Identity belongs to these exact IR bytes. It does NOT denote
                // a binary address or survive optimization/recompilation.
                // Ordinals include all functions, blocks and instructions,
                // so same-line calls and unnamed values remain distinguishable.
                const std::string SiteID = FunctionID + ":b" + std::to_string(BlockIndex)
                                             + ":i" + std::to_string(Index);
                std::string Operand;
                raw_string_ostream OperandStream(Operand);
                Call->getCalledOperand()->printAsOperand(OperandStream, false, Input.IR.get());

                Sites.push_back(json::Object{
                    {"callsite_id", SiteID},
                    {"module_id", Input.Digest},
                    {"function", json::Object{
                        {"id", FunctionID},
                        {"name", Function.hasName() ? json::Value(Function.getName())
                                                   : json::Value(nullptr)}}},
                    {"basic_block_index", static_cast<int64_t>(BlockIndex)},
                    {"instruction_index", static_cast<int64_t>(Index)},
                    {"opcode", Instruction.getOpcodeName()},
                    {"called_operand", Operand},
                    {"source_location", sourceLocation(Instruction)},
                    // Null is deliberately different from an analyzed empty
                    // target set. No backend or target estimation runs here.
                    {"target_analysis", json::Object{
                        {"status", "not_analyzed"},
                        {"backend", nullptr},
                        {"possible_callees", nullptr}}}});
            }
            ++BlockIndex;
        }
    }
}

static bool writeOutput(const json::Value &Document)
{
    if (Output == "-") {
        outs() << formatv("{0:2}\n", Document);
        outs().flush();
        return !outs().has_error();
    }

    // Commit only a complete document. Parsing or I/O errors must not destroy
    // a previously generated result.
    SmallString<256> Temporary;
    int Descriptor;
    std::error_code Error = sys::fs::createUniqueFile(Output + ".tmp-%%%%%%", Descriptor, Temporary);
    if (Error) {
        errs() << "icall_ir_scan: cannot create output: " << Error.message() << '\n';
        return false;
    }
    {
        raw_fd_ostream Stream(Descriptor, true);
        Stream << formatv("{0:2}\n", Document);
        Stream.close();
        if (Stream.has_error()) {
            errs() << "icall_ir_scan: cannot write output: " << Stream.error().message() << '\n';
            Stream.clear_error();
            sys::fs::remove(Temporary);
            return false;
        }
    }
    if ((Error = sys::fs::rename(Temporary, Output))) {
        errs() << "icall_ir_scan: cannot replace output: " << Error.message() << '\n';
        sys::fs::remove(Temporary);
        return false;
    }
    return true;
}

int main(int argc, char **argv)
{
    InitLLVM Init(argc, argv);
    cl::HideUnrelatedOptions(Category);
    cl::ParseCommandLineOptions(argc, argv,
        "Discover LLVM IR indirect callsites (no target estimation or binary mapping).\n");

    std::vector<InputModule> Modules;
    std::set<std::string> Digests;
    for (const std::string &Path : Inputs) {
        if (Output != "-" && sys::fs::equivalent(Path, Output)) {
            errs() << "icall_ir_scan: output must not overwrite an input: " << Path << '\n';
            return 1;
        }
        auto Buffer = MemoryBuffer::getFile(Path);
        if (!Buffer) {
            errs() << "icall_ir_scan: cannot read " << Path << ": "
                   << Buffer.getError().message() << '\n';
            return 1;
        }
        InputModule Input;
        Input.Path = Path;
        Input.Digest = fingerprint((*Buffer)->getBuffer());
        if (!Digests.insert(Input.Digest).second) {
            errs() << "icall_ir_scan: duplicate IR input contents: " << Path << '\n';
            return 1;
        }
        Input.Context = std::make_unique<LLVMContext>();
        SMDiagnostic Diagnostic;
        Input.IR = parseIR((*Buffer)->getMemBufferRef(), Diagnostic, *Input.Context);
        if (!Input.IR) {
            Diagnostic.print(argv[0], errs());
            return 1;
        }
        if (verifyModule(*Input.IR, &errs())) {
            errs() << "icall_ir_scan: invalid LLVM IR: " << Path << '\n';
            return 1;
        }
        Modules.push_back(std::move(Input));
    }
    std::sort(Modules.begin(), Modules.end(), [](const InputModule &A, const InputModule &B) {
        return A.Digest < B.Digest;
    });

    std::string BuildMaterial = "icallcov.llvm-ir-input-set.v1\n";
    json::Array ModuleRecords;
    json::Array Sites;
    for (const InputModule &Input : Modules) {
        BuildMaterial += Input.Digest + "\n";
        ModuleRecords.push_back(json::Object{
            {"module_id", Input.Digest},
            {"input_file", Input.Path},
            {"source_file", Input.IR->getSourceFileName()},
            {"target_triple", Input.IR->getTargetTriple().str()},
            {"data_layout", Input.IR->getDataLayoutStr()}});
        scanModule(Input, Sites);
    }
    const int64_t Count = static_cast<int64_t>(Sites.size());
    json::Object Document{
        {"kind", "llvm-ir-callsites"},
        {"schema_version", 1},
        {"llvm_version", LLVM_VERSION_STRING},
        // This fingerprints the supplied IR set, NOT an ELF build-id and
        // NOT proof that any separately built executable matches this IR.
        {"build_id", fingerprint(BuildMaterial)},
        {"id_scope", "ir-input-artifacts"},
        {"modules", std::move(ModuleRecords)},
        {"count", Count},
        {"indirect_callsites", std::move(Sites)}};
    return writeOutput(json::Value(std::move(Document))) ? 0 : 1;
}
