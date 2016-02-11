#
# Strelka - Small Variant Caller
# Copyright (c) 2009-2016 Illumina, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#

"""
Pedicure de-novo small variant calling workflow
"""


import os.path
import sys

# add this path to pull in utils in same directory:
scriptDir=os.path.abspath(os.path.dirname(__file__))
sys.path.append(scriptDir)

# add pyflow path:
sys.path.append(os.path.join(scriptDir,"pyflow"))


from configBuildTimeInfo import workflowVersion
from configureUtil import safeSetBool, getIniSections, dumpIniSections
from pyflow import WorkflowRunner
from sharedWorkflow import getMkdirCmd, getRmdirCmd, runDepthFromAlignments
from starkaWorkflow import runCount, StarkaCallWorkflow, StarkaWorkflow
from workflowUtil import checkFile, ensureDir, preJoin, which, \
                         getNextGenomeSegment, bamListCatCmd


__version__ = workflowVersion



def pedicureRunDepthFromAlignments(self,taskPrefix="getChromDepth",dependencies=None):
    assert len(self.params.probandBamList) == 1
    bamList = [ self.params.probandBamList[0] ]


    outputPath=self.paths.getChromDepth()
    return runDepthFromAlignments(self, bamList, outputPath, taskPrefix, dependencies)



class TempSegmentFiles :
    def __init__(self) :
        self.denovo = []
        self.callable = []



def callGenomeSegment(self, gseg, segFiles, taskPrefix="", dependencies=None) :

    isFirstSegment = (len(segFiles.denovo) == 0)

    segStr = str(gseg.id)

    segCmd = [ self.params.pedicureBin ]
    segCmd.append("-clobber")
    segCmd.extend(["-min-paired-align-score",str(self.params.minTier1Mapq)])
    segCmd.extend(["-min-single-align-score","10"])

#    segCmd.extend(["-min-qscore","0"])  # consider this once we go back to a quality based scoring model
    segCmd.extend(['-min-qscore','17'])

    # next three parameters describe same quality dependency structure as starling
    segCmd.extend(['-bsnp-ssd-no-mismatch', '0.35'])
    segCmd.extend(['-bsnp-ssd-one-mismatch', '0.6'])
    segCmd.extend(['-min-vexp', '0.25'])

    segCmd.extend(["-report-range-begin", str(gseg.beginPos) ])
    segCmd.extend(["-report-range-end", str(gseg.endPos) ])
    segCmd.extend(["-samtools-reference", self.params.referenceFasta ])
    segCmd.extend(["-max-window-mismatch", "3", "20" ])
    segCmd.extend(["-bam-seq-name", gseg.chromLabel] )
    segCmd.extend(["-genome-size", str(self.params.knownSize)] )
    segCmd.extend(["-max-indel-size", "50"] )
    segCmd.extend(["-indel-nonsite-match-prob", "0.5"] )

    segCmd.extend(["--tier2-min-single-align-score", str(self.params.minTier2Mapq) ] )
    segCmd.extend(["--tier2-min-paired-align-score", str(self.params.minTier2Mapq) ] )
    segCmd.extend(["--tier2-mismatch-density-filter-count", "10"] )
    segCmd.append("--tier2-no-filter-unanchored")
    segCmd.extend(["--tier2-indel-nonsite-match-prob", "0.25"] )
    segCmd.append("--tier2-include-singleton")
    segCmd.append("--tier2-include-anomalous")

    tmpDenovoPath = self.paths.getTmpSegmentDenovoPath(segStr)
    segFiles.denovo.append(tmpDenovoPath+".gz")
    segCmd.extend(["--denovo-file",tmpDenovoPath])

    for bamPath in self.params.probandBamList :
        segCmd.extend(["--proband-align-file", bamPath])
    for bamPath in self.params.parentBamList :
        segCmd.extend(["--parent-align-file", bamPath])
    for bamPath in self.params.siblingBamList :
        segCmd.extend(["--sibling-align-file", bamPath])

    if self.params.isWriteCallableRegion :
        tmpCallablePath = self.paths.getTmpSegmentRegionPath(segStr)
        segFiles.callable.append(tmpCallablePath+".gz")
        segCmd.extend(["--denovo-callable-region-file", tmpCallablePath ])

    def addListCmdOption(optList,arg) :
        if optList is None : return
        for val in optList :
            segCmd.extend([arg, val])

    addListCmdOption(self.params.indelCandidatesList, '--candidate-indel-input-vcf')

    if self.params.extraCallerArguments is not None :
        for arg in self.params.extraCallerArguments.strip().split() :
            segCmd.append(arg)

    segCmd.extend(["--report-file", self.paths.getTmpSegmentReportPath(gseg.pyflowId)])

    if not isFirstSegment :
        segCmd.append("--pedicure-skip-header")

    if self.params.isHighDepthFilter :
        segCmd.extend(["--pedicure-chrom-depth-file", self.paths.getChromDepth()])
        segCmd.extend(["--pedicure-max-depth-factor", self.params.depthFilterMultiple])

    nextStepWait = set()

    callTask=preJoin(taskPrefix,"callGenomeSegment_"+gseg.pyflowId)
    self.addTask(callTask,segCmd,dependencies=dependencies,memMb=self.params.callMemMb)

    # fix vcf header to use parent pyflow cmdline instead of random segment command:
    compressWaitFor=callTask
    if isFirstSegment :
        headerFixTask=preJoin(taskPrefix,"fixVcfHeader_"+gseg.pyflowId)
        def getHeaderFixCmd(fileName) :
            tmpName=fileName+".reheader.tmp"
            cmd  = "\"%s\" -E \"%s\"" % (sys.executable, self.params.vcfCmdlineSwapper)
            cmd += ' "' + " ".join(self.params.configCommandLine) + '"'
            cmd += " < \"%s\" > \"%s\" && mv \"%s\" \"%s\"" % (fileName,tmpName,
                                                               tmpName, fileName)
            return cmd

        headerFixCmd  = getHeaderFixCmd(tmpDenovoPath)

        self.addTask(headerFixTask, headerFixCmd, dependencies=callTask, isForceLocal=True)
        compressWaitFor=headerFixTask

    compressTask=preJoin(taskPrefix,"compressSegmentOutput_"+gseg.pyflowId)
    compressCmd="\"%s\" \"%s\"" % (self.params.bgzipBin, tmpDenovoPath)
    if self.params.isWriteCallableRegion :
        compressCmd += " && \"%s\" \"%s\"" % (self.params.bgzipBin, self.paths.getTmpSegmentRegionPath(segStr))

    self.addTask(compressTask, compressCmd, dependencies=compressWaitFor, isForceLocal=True)
    nextStepWait.add(compressTask)

    return nextStepWait



def callGenome(self,taskPrefix="",dependencies=None):
    """
    run variant caller on all genome segments
    """

    tmpSegmentDir=self.paths.getTmpSegmentDir()
    dirTask=self.addTask(preJoin(taskPrefix,"makeTmpDir"), getMkdirCmd() + [tmpSegmentDir], dependencies=dependencies, isForceLocal=True)

    segmentTasks = set()

    segFiles = TempSegmentFiles()
    for gseg in getNextGenomeSegment(self.params) :

        segmentTasks |= callGenomeSegment(self, gseg, segFiles, dependencies=dirTask)

    if len(segmentTasks) == 0 :
        raise Exception("No genome regions to analyze. Possible target region parse error.")

    # create a checkpoint for all segments:
    completeSegmentsTask = self.addTask(preJoin(taskPrefix,"completedAllGenomeSegments"),dependencies=segmentTasks)

    finishTasks = set()

    finishTasks.add(self.concatIndexVcf(taskPrefix, completeSegmentsTask, segFiles.denovo,
                                        self.paths.getDenovoOutputPath(),"denovo"))

    if self.params.isWriteCallableRegion :
        finishTasks.add(self.concatIndexBed(taskPrefix, completeSegmentsTask, segFiles.callable,
                                            self.paths.getRegionOutputPath(), "callableRegions"))

    if not self.params.isRetainTempFiles :
        rmStatsTmpCmd = getRmdirCmd() + [tmpSegmentDir]
        rmTask=self.addTask(preJoin(taskPrefix,"rmTmpDir"),rmStatsTmpCmd,dependencies=finishTasks, isForceLocal=True)

    nextStepWait = finishTasks

    return nextStepWait



"""
A separate call workflow is setup so that we can delay the workflow execution until
the ref count file exists
"""
class CallWorkflow(StarkaCallWorkflow) :

    def __init__(self,params,paths) :
        super(CallWorkflow,self).__init__(params)
        self.paths = paths

    def workflow(self) :

        if True :
            knownSize = 0
            for line in open(self.paths.getRefCountFile()) :
                word = line.strip().split('\t')
                if len(word) != 4 :
                    raise Exception("Unexpected format in ref count file: '%s'" % (self.paths.getRefCountFile()))
                knownSize += int(word[2])

            self.params.knownSize = knownSize

        callGenome(self)



class PathInfo:
    """
    object to centralize shared workflow path names
    """

    def __init__(self, params) :
        self.params = params

    def getChromDepth(self) :
        return os.path.join(self.params.workDir,"chromDepth.txt")

    def getTmpSegmentDir(self) :
        return os.path.join(self.params.workDir, "genomeSegment.tmpdir")

    def getTmpSegmentDenovoPath(self, segStr) :
        return os.path.join( self.getTmpSegmentDir(), "denovo.unfiltered.%s.vcf" % (segStr))

    def getTmpSegmentRegionPath(self, segStr) :
        return os.path.join( self.getTmpSegmentDir(), "denovo.callable.region.%s.bed" % (segStr))

    def getTmpUnsortRealignBamPath(self, segStr, label) :
        return os.path.join( self.getTmpSegmentDir(), "%s.%s.unsorted.realigned.bam" % (label, segStr))

    def getTmpRealignBamPath(self, segStr, label) :
        return os.path.join( self.getTmpSegmentDir(), "%s.%s.realigned.bam" % (label, segStr))

    def getTmpSegmentReportPath(self, segStr) :
        return os.path.join( self.getTmpSegmentDir(), "stats.%s.txt" % (segStr))

    def getVariantsDir(self) :
        return self.params.variantsDir

    def getDenovoOutputPath(self) :
        return os.path.join( self.getVariantsDir(), "denovo.vcf.gz")

    def getRegionOutputPath(self) :
        return os.path.join( self.params.regionsDir, 'denovo.callable.region.bed.gz');

    def getRealignedBamPath(self, label) :
        return os.path.join( self.params.realignedDir, '%s.realigned.bam' % (label));

    def getRefCountFile(self) :
        return os.path.join( self.params.workDir, "refCount.txt")



class PedicureWorkflow(StarkaWorkflow) :
    """
    Pedicure de-novo small variant calling workflow
    """

    def __init__(self,params,iniSections) :

        super(PedicureWorkflow,self).__init__(params,iniSections)

        # format bam lists:
        if self.params.probandBamList is None : self.params.probandBamList = []
        if self.params.parentBamList is None : self.params.parentBamList = []
        if self.params.siblingBamList is None : self.params.siblingBamList = []

        # bools coming from the ini file need to be cleaned up:
        safeSetBool(self.params,"isWriteRealignedBam")

        if self.params.isWriteCallableRegion :
            self.params.regionsDir=os.path.join(self.params.resultsDir,"regions")
            ensureDir(self.params.regionsDir)

#        if self.params.isWriteRealignedBam :
#            self.params.realignedDir=os.path.join(self.params.resultsDir,"realigned")
#            ensureDir(self.params.realignedDir)

        self.paths = PathInfo(self.params)



    def getSuccessMessage(self) :
        "Message to be included in email for successful runs"

        msg  = "Pedicure de-novo variant workflow successfully completed.\n\n"
        msg += "\tworkflow version: %s\n" % (__version__)
        return msg



    def workflow(self) :
        self.flowLog("Initiating Pedicure workflow version: %s" % (__version__))
        self.setCallMemMb()

        callPreReqs = set()
        callPreReqs |= runCount(self)
        if self.params.isHighDepthFilter :
            callPreReqs |= pedicureRunDepthFromAlignments(self)

        self.addWorkflowTask("CallGenome", CallWorkflow(self.params, self.paths), dependencies=callPreReqs)
