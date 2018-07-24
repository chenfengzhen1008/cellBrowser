#!/usr/bin/env python2

# requires at least python2.6, version tested was 2.6.6
# should work with python2.5, not tested
# works on python3, version tested was 3.6.5

import logging, sys, optparse, struct, json, os, string, shutil, gzip, re, unicodedata
import zlib, math, operator, doctest, copy, bisect, array, glob, io, time
from collections import namedtuple, OrderedDict
from os.path import join, basename, dirname, isfile, isdir, relpath, abspath

# python2.6 has no defaultdict or Counter yet
try:
    from collections import defaultdict
    from collections import Counter
except:
    from backport_collections import defaultdict # error? -> pip2 install backport-collections
    from backport_collections import Counter # error? -> pip2 install backport-collections

try:
    from future.utils import iteritems
except:
    from six import iteritems # error? pip2 install six

# Does not require numpy but numpy is around 30-40% faster in serializing arrays
numpyLoaded = True
try:
    import numpy as np
except:
    numpyLoaded = False
    logging.warn("Numpy could not be loaded. The script should work, but it will be a lot slower to process the matrix.")

# older numpy versions don't have tobytes()
try:
    np.ndarray.tobytes
except:
    numpyLoaded = False
    logging.warn("Numpy version too old. Falling back to normal Python array handling.")

isPy3 = False
if sys.version_info >= (3, 0):
    isPy3 = True

# directory to static data files, e.g. gencode tables
dataDir = join(dirname(__file__), "static")

defOutDir = os.environ.get("CBOUT")

# ==== functions =====
    
def parseArgs(showHelp=False):
    " setup logging, parse command line arguments and options. -h shows auto-generated help page "
    parser = optparse.OptionParser("usage: %prog [options] -i dataset.conf -o outputDir - add a dataset to the single cell viewer directory")

    parser.add_option("-d", "--debug", dest="debug", action="store_true",
        help="show debug messages")

    parser.add_option("-i", "--inConf", dest="inConf", action="store",
        help="a dataset.conf file that specifies labels and all input files, default %default", default="dataset.conf")

    parser.add_option("-o", "--outDir", dest="outDir", action="store", help="output directory, default can be set through the env. variable CBOUT, current value: %default", default=defOutDir)

    parser.add_option("-q", "--quick",
        dest="quick",
        action="store_true", help="Do not rebuild the big gene expression files, if they already exist")
    parser.add_option("", "--test",
        dest="test",
        action="store_true", help="run a few tests")

    (options, args) = parser.parse_args()

    if showHelp:
        parser.print_help()
        exit(1)

    if options.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    return args, options

def makeDir(outDir):
    if not isdir(outDir):
        logging.info("Creating %s" % outDir)
        os.makedirs(outDir)

def errAbort(msg, showHelp=False):
        logging.error(msg)
        if showHelp:
            parseArgs(showHelp=True)
        sys.exit(1)

def lineFileNextRow(inFile):
    """
    parses tab-sep file with headers in first line
    yields collection.namedtuples
    strips "#"-prefix from header line
    """

    if isinstance(inFile, str):
        # input file is a string = file name
        fh = openFile(inFile)
        sep = sepForFile(inFile)
    else:
        fh = inFile
        sep = "\t"

    line1 = fh.readline()
    line1 = line1.strip("\n").lstrip("#")
    headers = line1.split(sep)
    headers = [re.sub("[^a-zA-Z0-9_]","_", h) for h in headers]
    headers = [re.sub("^_","", h) for h in headers] # remove _ prefix
    #headers = [x if x!="" else "noName" for x in headers]
    if headers[0]=="": # R does not name the first column by default
        headers[0]="rowName"

    if "" in headers:
        logging.error("Found empty cells in header line of %s" % inFile)
        logging.error("This often happens with Excel files. Make sure that the conversion from Excel was done correctly. Use cut -f-lastColumn to fix it.")
        assert(False)

    filtHeads = []
    for h in headers:
        if h[0].isdigit():
            filtHeads.append("x"+h)
        else:
            filtHeads.append(h)
    headers = filtHeads


    Record = namedtuple('tsvRec', headers)
    for line in fh:
        if line.startswith("#"):
            continue
        #line = line.decode("latin1")
        # skip special chars in meta data and keep only ASCII
        #line = unicodedata.normalize('NFKD', line).encode('ascii','ignore')
        line = line.rstrip("\n").rstrip("\r")
        if isPy3:
            fields = line.split(sep, maxsplit=len(headers)-1)
        else:
            fields = string.split(line, sep, maxsplit=len(headers)-1)

        try:
            rec = Record(*fields)
        except Exception as msg:
            logging.error("Exception occured while parsing line, %s" % msg)
            logging.error("Filename %s" % fh.name)
            logging.error("Line was: %s" % line)
            logging.error("Does number of fields match headers?")
            logging.error("Headers are: %s" % headers)
            raise Exception("header count: %d != field count: %d wrong field count in line %s" % (len(headers), len(fields), line))
        yield rec

def parseOneColumn(fname, colName):
    " return a single column from a tsv as a list "
    ifh = open(fname)
    sep = "\t"
    headers = ifh.readline().rstrip("\n").rstrip("\r").split(sep)
    colIdx = headers.index(colName)
    vals = []
    for line in ifh:
        row = line.rstrip("\n").rstrip("\r").split(sep)
        vals.append(row[colIdx])
    return vals

def parseIntoColumns(fname):
    " parse tab sep file vertically, return as a list of (headerName, list of values) "
    ifh = open(fname)
    sep = "\t"
    headers = ifh.readline().rstrip("\n").rstrip("\r").split(sep)
    colsToGet = range(len(headers))

    columns = []
    for h in headers:
        columns.append([])

    for line in ifh:
        row = line.rstrip("\n").rstrip("\r").split(sep)
        for colIdx in colsToGet:
            columns[colIdx].append(row[colIdx])
    return zip(headers, columns)

def openFile(fname, mode="rt"):
    if fname.endswith(".gz"):
        if isPy3:
            fh = gzip.open(fname, mode, encoding="latin1")
        else:
            fh = gzip.open(fname, mode)
    else:
        if isPy3:
            fh = io.open(fname, mode)
        else:
            fh = open(fname, mode)
    return fh

def parseDict(fname):
    """ parse text file in format key<tab>value and return as dict key->val """
    d = {}

    fh = openFile(fname)

    sep = "\t"
    if fname.endswith(".csv"):
        sep = ","

    for line in fh:
        key, val = line.rstrip("\n").split(sep)
        d[key] = val
    return d

def readGeneToSym(fname):
    " given a file with geneId,symbol return a dict geneId -> symbol. Strips anything after . in the geneId "
    if fname.lower()=="none":
        return None

    logging.info("Reading gene,symbol mapping from %s" % fname)

    # Jim's files and CellRanger files have no headers, they are just key-value
    line1 = open(fname).readline()
    if "geneId" not in line1:
        d = parseDict(fname)
    # my new gencode tables contain a symbol for ALL genes
    elif line1=="transcriptId\tgeneId\tsymbol":
        for row in lineFileNextRow(fname):
            if row.symbol=="":
                continue
            d[row.geneId.split(".")[0]]=row.symbol
    # my own files have headers
    else:
        d = {}
        for row in lineFileNextRow(fname):
            if row.symbol=="":
                continue
            d[row.geneId.split(".")[0]]=row.symbol
    return d

def getDecilesList_np(values):
    deciles = np.percentile( values, [0,10,20,30,40,50,60,70,80,90,100] )
    return deciles

def bytesAndFmt(x):
    """ how many bytes do we need to store x values and what is the sprintf
    format string for it?
    """

    if x > 65535:
        assert(False) # field with more than 65k elements or high numbers? Weird meta data.

    if x > 255:
        return "Uint16", "<H" # see javascript typed array names, https://developer.mozilla.org/en-US/docs/Web/JavaScript/Typed_arrays
    else:
        return "Uint8", "<B"

#def getDecilesWithZeros(numVals):
#    """ return a pair of the deciles and their counts.
#    Counts is 11 elements long, the first element holds the number of zeros, 
#    which are treated separately
#
#    >>> l = [0,0,0,0,0,0,0,0,0,0,0,0,1,2,3,4,5,6,7,8,9,10]
#    >>> getDecilesWithZeros(l)
#     ([1, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10], [12, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1])
#    """
#    nonZeros  = [x for x in numVals if x!=0.0]
#
#    zeroCount = len(numVals) - len(nonZeros)
#    deciles   = getDecilesList_np(nonZeros)
#
#    decArr = np.searchsorted(deciles, numVals)
#    decCounts(deciles, nonZeros)
#
#    decCounts.insert(0, zeroCount)
#    return deciles, decCounts, newVals

def findBins(numVals, breakVals):
    """
    find the right bin index defined by breakVals for every value in numVals.
    Special handling for the last value. The comparison uses "<=". The first
    break is assumed to be the minimum of numVals and is therefore ignored.
    Also returns an array with the count for every bin.
    >>> findBins([1,1,1,2,2,2,3,3,4,4,5,5,6,6], [1, 2,3,5,6])
    ([0, 0, 0, 0, 0, 0, 1, 1, 2, 2, 2, 2, 3, 3], [6, 2, 4, 2])
    """
    breaks = breakVals[1:]
    bArr = []
    binCounts = [0]*len(breaks)
    for x in numVals:
        binIdx = bisect.bisect_left(breaks, x)
        bArr.append(binIdx)
        binCounts[binIdx]+=1
    return bArr, binCounts

def countBinsBetweenBreaks(numVals, breakVals):
    """ count how many numVals fall into the bins defined by breakVals.
    Special handling for the last value. Comparison uses "<=". The first
    break is assumed to be the minimum of numVals.
    Also returns an array with the bin for every element in numVals
    >>> countBinsBetweenBreaks([1,1,1,2,2,2,3,3,4,4,5,5,6,6], [1,2,3,5,6])
    ([6, 2, 4, 2], [0, 0, 0, 0, 0, 0, 1, 1, 2, 2, 2, 2, 3, 3])
    """

    binCounts = []
    binCount = 0
    i = 1
    dArr = []
    for x in numVals:
        if x <= breakVals[i]:
            binCount+=1
        else:
            binCounts.append(binCount)
            binCount = 1
            i += 1
        dArr.append(i-1)

    binCounts.append(binCount)

    assert(len(dArr)==len(numVals))
    assert(len(binCounts)==len(breakVals)-1)
    return binCounts, dArr

def discretizeArray(numVals, fieldMeta):
    """
    discretize numeric values based on quantiles.
    """
    maxBinCount = 10
    counts = Counter(numVals).most_common()
    counts.sort() # sort by value, not count

    if len(counts) < maxBinCount:
        # if we have just a few values, do not do any binning
        binCounts = [y for x,y in counts]
        values = [x for x,y in counts]

        valToBin = {}
        for i, x in values:
            valToBin[x] = i

        dArr = [valToBin[x] for x in numVals]

        fieldMeta["binMethod"] = "raw"
        fieldMeta["values"] = values
        fieldMeta["binCounts"] = binCounts
        return dArr, fieldMeta

    # ten breaks
    breakPercs = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
    countLen = len(counts)
    breakIndices = [int(round(bp*countLen)) for bp in breakPercs]
    # as with all histograms, the last break is always a special case (0-based array)
    breakIndices.append(countLen-1)
    breakVals = [counts[idx][0] for idx in breakIndices]

    dArr, binCounts = findBins(numVals, breakVals)
    assert(len(binCounts)==10)
    logging.info("Number of values per decile-bin: %s" % binCounts)

    fieldMeta["binMethod"] = "quantiles"
    fieldMeta["binCounts"] = binCounts
    fieldMeta["breaks"] = breakVals

    return dArr, fieldMeta

def discretizeNumField(numVals, fieldMeta, numType):
    " given a list of numbers, add attributes to fieldMeta that describe the binning scheme "
    #digArr, fieldMeta = discretizeArr_uniform(numVals, fieldMeta)
    digArr, fieldMeta = discretizeArray(numVals, fieldMeta)

    #deciles, binCounts, newVals = getDecilesWithZeros(numVals)

    fieldMeta["arrType"] = "uint8"
    fieldMeta["_fmt"] = "<B"
    return digArr, fieldMeta

def guessFieldMeta(valList, fieldMeta, colors, forceEnum):
    """ given a list of strings, determine if they're all int, float or
    strings. Return fieldMeta, as dict, and a new valList, with the correct python type
    - 'type' can be: 'int', 'float', 'enum' or 'uniqueString'
    - if int or float: 'deciles' is a list of the deciles
    - if uniqueString: 'maxLen' is the length of the longest string
    - if enum: 'values' is a list of all possible values
    - if colors is not None: 'colors' is a list of the default colors
    """
    intCount = 0
    floatCount = 0
    valCounts = defaultdict(int)
    #maxVal = 0
    for val in valList:
        fieldType = "string"
        try:
            newVal = int(val)
            intCount += 1
            floatCount += 1
            #maxVal = max(newVal, val)
        except:
            try:
                newVal = float(val)
                floatCount += 1
                #maxVal = max(newVal, val)
            except:
                pass

        valCounts[val] += 1

    valToInt = None

    if floatCount==len(valList) and intCount!=len(valList) and len(valCounts) > 10 and not forceEnum:
        # field is a floating point number: convert to decile index
        numVals = [float(x) for x in valList]

        newVals, fieldMeta = discretizeNumField(numVals, fieldMeta, "float")

        fieldMeta["type"] = "float"
        #fieldMeta["maxVal"] = maxVal

    elif intCount==len(valList) and not forceEnum:
        # field is an integer: convert to decile index
        numVals = [int(x) for x in valList]
        newVals, fieldMeta = discretizeNumField(numVals, fieldMeta, "int")
        fieldMeta["type"] = "int"
        #fieldMeta["maxVal"] = maxVal

    elif len(valCounts)==len(valList) and not forceEnum:
        # field is a unique string
        fieldMeta["type"] = "uniqueString"
        maxLen = max([len(x) for x in valList])
        fieldMeta["maxSize"] = maxLen
        fieldMeta["_fmt"] = "%ds" % (maxLen+1)
        newVals = valList

    else:
        # field is an enum - convert to enum index
        fieldMeta["type"] = "enum"
        valArr = list(valCounts.keys())

        if colors!=None:
            colArr = []
            foundColors = 0
            notFound = set()
            for val in valArr:
                if val in colors:
                    colArr.append(colors[val])
                    foundColors +=1
                else:
                    notFound.add(val)
                    colArr.append("DDDDDD") # wonder if I should not stop here
            if foundColors > 0:
                fieldMeta["colors"] = colArr
                if len(notFound)!=0:
                    logging.warn("No default color found for field values %s" % notFound)

        valCounts = list(sorted(valCounts.items(), key=operator.itemgetter(1), reverse=True)) # = (label, count)
        fieldMeta["valCounts"] = valCounts
        fieldMeta["arrType"], fieldMeta["_fmt"] = bytesAndFmt(len(valArr))
        valToInt = dict([(y[0],x) for (x,y) in enumerate(valCounts)]) # dict with value -> index in valCounts
        newVals = [valToInt[x] for x in valList] # 

    #fieldMeta["valCount"] = len(valList)
    fieldMeta["diffValCount"] = len(valCounts)

    return fieldMeta, newVals

def writeNum(col, packFmt, ofh):
    " write a list of numbers to a binary file "

def cleanString(s):
    " returns only alphanum characters in string s "
    newS = []
    for c in s:
        if c.isalnum():
            newS.append(c)
    return "".join(newS)

def metaToBin(fname, colorFname, outDir, enumFields, datasetInfo):
    """ convert meta table to binary files. outputs fields.json and one binary file per field. 
    adds names of metadata fields to datasetInfo and returns datasetInfo
    """
    makeDir(outDir)

    colData = parseIntoColumns(fname)

    colors = parseColors(colorFname)

    fieldInfo = []
    for colIdx, (fieldName, col) in enumerate(colData):
        logging.info("Meta data field index %d: '%s'" % (colIdx, fieldName))

        forceEnum = False
        if enumFields!=None:
            forceEnum = (fieldName in enumFields)
        cleanFieldName = cleanString(fieldName)
        binName = join(outDir, cleanFieldName+".bin")

        fieldMeta = OrderedDict()
        fieldMeta["name"] = cleanFieldName
        fieldMeta["label"] = fieldName
        fieldMeta, binVals = guessFieldMeta(col, fieldMeta, colors, forceEnum)
        fieldType = fieldMeta["type"]

        if "metaOpt" in datasetInfo and fieldName in datasetInfo["metaOpt"]:
            fieldMeta["opt"] = datasetInfo["metaOpt"][fieldName]

        packFmt = fieldMeta["_fmt"]

        # write the binary file
        binFh = open(binName, "wb")
        if fieldMeta["type"]!="uniqueString":
            for x in binVals:
                binFh.write(struct.pack(packFmt, x))
        else:
            for x in col:
                if isPy3:
                    binFh.write(bytes("%s\n" % x, encoding="ascii"))
                else:
                    binFh.write("%s\n" % x)
        binFh.close()

        del fieldMeta["_fmt"]
        fieldInfo.append(fieldMeta)
        if "type" in fieldMeta:
            logging.info(("Type: %(type)s, %(diffValCount)d different values" % fieldMeta))
        else:
            logging.info(("Type: %(type)s, %(diffValCount)d different values, max size %(maxSize)d " % fieldMeta))

    if "metaOpt" in datasetInfo:
        del datasetInfo["metaOpt"] # don't need this anymore

    datasetInfo["metaFields"] = fieldInfo
    return datasetInfo

def iterLineOffsets(ifh):
    """ parse a text file and yield tuples of (line, startOffset, endOffset).
    endOffset does not include the newline, but the newline is not stripped from line.
    """
    line = True
    start = 0
    while line!='':
       line = ifh.readline()
       end = ifh.tell()-1
       if line!="":
           yield line, start, end
       start = ifh.tell()

def iterExprFromFile(fname, matType, geneToSym):
    " yield (gene, symbol, array) tuples from gene expression file "
    if fname.endswith(".gz"):
        #ifh = gzip.open(fname)
        ifh = os.popen("zcat -q "+fname+" 2> /dev/null") # faster, especially with two CPUs
    else:
        ifh = open(fname)
    
    sep = "\t"
    if ".csv" in fname.lower():
        sep = ","

    if matType == "float":
        npType = "float32"
    else:
        npType = "int32"

    headLine = ifh.readline()
    skipIds = 0
    doneGenes = set()
    for line in ifh:
        if isPy3:
            gene, rest = line.split(sep, maxsplit=1)
        else:
            gene, rest = string.split(line, sep, maxsplit=1)

        if numpyLoaded:
            a = np.fromstring(rest, sep=sep, dtype=npType)
        else:
            if matType=="int":
                a = [int(x) for x in rest.split(sep)]
            else:
                a = [float(x) for x in rest.split(sep)]

        if geneToSym is None:
            symbol = gene
        else:
            symbol = geneToSym.get(gene)
            if symbol is None:
                skipIds += 1
                logging.warn("%s is not a valid Ensembl gene ID, check geneIdType setting in dataset.conf" % geneId)
                continue
            if symbol.isdigit():
                logging.warn("line %d in gene matrix: gene identifier %s is a number. If this is indeed a gene identifier, you can ignore this warning." (geneCount, symbol))

        if symbol in doneGenes:
            logging.warn("Gene %s/%s is duplicated in matrix, using only first occurence" % (geneId, symbol))
            skipIds += 1
            continue

        doneGenes.add(gene)

        yield gene, symbol, a
    
    if skipIds!=0:
        logging.warn("Skipped %d expression matrix lines because of duplication/unknown ID" % skipIds)


def autoDetectMatType(matIter, n):
    " check if matrix has 'int' or 'float' data type by looking at the first n genes"
    # auto-detect the type of the matrix: int vs float
    geneCount = 0

    for geneId, sym, a in matIter:
        if numpyLoaded:
            a_int = a.astype(int)
            hasOnlyInts = np.array_equal(a, a_int)
            if not hasOnlyInts:
                return "float"
        else:
            for x in a:
                frac, whole = math.modf(x)
                if frac != 0.0:
                    return "float"
        if geneCount==n:
            break
        geneCount+=1
    return "int"

def getDecilesList(values):
    """ given a list of values, return the 10 values that define the 10 ranges for the deciles
    """
    if len(values)==0:
        return None

    valCount = len(values)
    binSize = float(valCount-1) / 10.0; # width of each bin, in number of elements, fractions allowed

    values = list(sorted(values))

    # get deciles from the list of sorted values
    deciles = []
    pos = 0
    for i in range(10): # 10 bins means that we need 10 limits, the last limit is at 90%
        pos = int (binSize * i)
        if pos > valCount: # this should not happen, but maybe it can, due to floating point issues?
            logging.warn("decile exceeds 10, binSize %d, i %d, len(values) %d" % (binSize, i, len(values)))
            pos = len(values)
        deciles.append ( values[pos] )
    return deciles

def findBin(ranges, val):
    """ given an array of values, find the index i where ranges[i] < val <= ranges[i+1]
    ranges have to be sorted.
    This is a dumb brute force implementation - maybe binary search is faster, if ever revisit this again
    Someone said up to 10 binary search is not faster.
    """
    if val==0: # speedup
        return 0
    for i in range(0, len(ranges)):
        if (val < ranges[i]):
            return i
    # if doesn't fit in anywhere, return beyond last possible index
    return i+1

def discretizeArr_uniform(arr, fieldMeta):
    """ given an array of numbers, get min/max, create 10 bins between min and max then
    translate the array to bins and return the list of bins
    """
    arrMin = min(arr)
    arrMax = max(arr)
    stepSize = (arrMax-arrMin)/10.0

    dArr = [0]*len(arr)
    binCounts = [0]*10
    for i, x in enumerate(arr):
        binIdx = int(round((x - arrMin)/stepSize))
        if x == arrMax:
            binIdx = 9
        assert(binIdx <= 9)
        dArr[i] = binIdx
        binCounts[binIdx]+=1

    fieldMeta["binMethod"] = "uniform"
    fieldMeta["minVal"] = arrMin
    fieldMeta["maxVal"] = arrMax
    fieldMeta["stepSize"] = stepSize
    fieldMeta["binCounts"] = binCounts
    return dArr, fieldMeta

def digitize_py(arr, matType):
    """ calculate deciles ignoring 0s from arr, use these deciles to digitize the whole arr,
    return (digArr, zeroCount, bins).
    bins is an array of (min, max, count)
    There are at most 11 bins and bin0 is just for the value zero.
    For bin0, min and max are both 0.0

    matType can be "int" or "float".
    If it is 'int' and arr has only <= 11 values, will not calculate deciles, but rather just
    count the numbers and use them to create bins, one per number.
    #>>> digitize_py([1,1,1,1,1,2,3,4,5,6,4,5,5,5,5], "float")
    """
    if matType=="int":
        valCount = len(set(arr))
        if valCount <= 11: # 10 deciles + 0s
            counts = Counter(arr).most_common()
            counts.sort()

            valToIdx = {}
            for i, (val, count) in enumerate(counts):
                valToIdx[val] = i

            digArr = [valToIdx[x] for x in arr]
            bins = []
            for val, count in counts:
                bins.append( (val, val, count) )
            return digArr, bins

    noZeroArr = [x for x in arr if x!=0]
    zeroCount = len(arr) - len(noZeroArr)
    deciles = getDecilesList(noZeroArr) # there are 10 limits for the 10 deciles, 0% - 90%
    deciles.insert(0, 0) # bin0 is always for the zeros
    # we now have 11 limits
    assert(len(deciles)<=11)

    # digitize and count bins
    digArr = []
    binCounts = len(deciles)*[0]
    for x in arr:
        binIdx = findBin(deciles, x)
        # bin1 is always empty, so move down all other indices
        if binIdx>0:
            binIdx-=1
        digArr.append(binIdx)
        binCounts[binIdx]+=1

    # create the bin info
    bins = []
    if zeroCount!=0:
        bins.append( [float(0), float(0), float(zeroCount)])

    for i in range(1, len(deciles)):
        minVal = deciles[i-1]
        maxVal = deciles[i]
        count = binCounts[i]
        # skip empty bins
        #if count!=0:
        bins.append( [float(minVal), float(maxVal), float(count)] )

    # add the maximum value explicitly, more meaningful
    bins[-1][1] = np.amax(arr)
    return digArr, bins

def digitizeArr(arr, numType):
    if numpyLoaded:
        return digitize_np(arr, numType)
    else:
        return digitize_py(arr, numType)

def binEncode(bins):
    " encode a list of at 11 three-tuples into a string of 33 floats (little endian)"
    # add (0,0,0) elements to bins until it has 11 elements "
    padBins = copy.copy(bins)
    for i in range(len(bins), 11):
        padBins.append( (0.0, 0.0, 0.0) )
    #print len(padBins), padBins, len(padBins)
    assert(len(padBins)==11)

    strList = []
    for xMin, xMax, count in padBins:
        strList.append( struct.pack("<f", xMin) )
        strList.append( struct.pack("<f", xMax) )
        strList.append( struct.pack("<f", count) )
    ret = "".join(strList)
    assert(len(ret)==11*3*4)
    return ret

def digitize_np(arr, matType):
    """ hopefully the same as digitize(), but using numpy 
    #>>> digitize_np([1,2,3,4,5,6,4,1,1,1], "int")
    #>>> digitize_np([0,0,0,1,1,1,1,1,2,3,4,5,6,4,5,5,5,5], "float")
    #>>> digitize_np([1,1,1,1,1,2,3,4,5,6,4,5,5,5,5], "float")
    """

    # meta data comes in as a list
    if not type(arr) is np.ndarray:
        arr = np.array(arr)

    if matType=="int":
        # raw counts mode:
        # first try if there are enough unique values in the array
        # if there are <= 10 values, deciles make no sense,
        # so simply enumerate the values and map to bins 0-10
        binCounts = np.bincount(arr)
        nonZeroCounts = binCounts[np.nonzero(binCounts)] # remove the 0s
        if nonZeroCounts.size <= 11:
            logging.debug("we have read counts and <11 values: not using quantiles, just enumerating")
            posWithValue = np.where(binCounts != 0)[0]
            valToBin = {}
            bins = []
            binIdx = 0
            #for val, count in enumerate(binCounts):
                #if count!=0:
            for val in posWithValue: 
                count = binCounts[val]
                bins.append( (val, val, count) )
                valToBin[val] = binIdx
                binIdx += 1
            # map values to bin indices, from stackoverflow
            digArr = np.vectorize(valToBin.__getitem__)(arr)
            return digArr, bins

    logging.debug("calculating deciles")
    # calculate the deciles without the zeros, otherwise
    # the 0s completely distort the deciles
    #noZero = np.copy(arr)
    #nonZeroIndices = np.nonzero(arr)
    noZero = arr[np.nonzero(arr)]

    # gene not expressed -> do nothing
    if noZero.size==0:
        logging.debug("expression vector is all zeroes")
        return np.zeros(arr.size, dtype=np.int8), [(0.0, 0.0, arr.size)]

    deciles = np.percentile( noZero, [0,10,20,30,40,50,60,70,80,90] , interpolation="lower")
    # make sure that we always have a bin for the zeros
    deciles = np.insert(deciles, 0, 0)
    logging.debug("deciles are: %s" % str(deciles))

    # now we have 10 limits, defining 11 bins
    # but bin1 will always be empty, as there is nothing between the value 0 and the lowest limit
    digArr = np.searchsorted(deciles, arr, side="right")
    # so we decrease all bin indices that are not 0
    np.putmask(digArr, digArr>0, digArr-1)
    binCounts = np.bincount(digArr)

    bins = []
    zeroCount = binCounts[0]

    # bin0 is a bit special
    if zeroCount!=0:
        bins.append( [float(0), float(0), zeroCount] )

    for i in range(1, len(deciles)):
        binCount = binCounts[i]
        #if binCount==0:
            #continue
        minVal = deciles[i-1]
        maxVal = deciles[i]
        bins.append( [minVal, maxVal, binCount] )

    bins[-1][1] = np.amax(arr)
    #print bins, len(digArr), digArr
    return digArr, bins

def maxVal(a):
    if numpyLoaded:
        return np.amax(a)
    else:
        return max(a)

def discretExprRowEncode(geneDesc, binInfo, digArr):
    " encode geneDesc, deciles and array of decile indixes into a single string that can be read by the .js code "
    # The format of a record is:
    # - 2 bytes: length of descStr, e.g. gene identifier or else
    # - len(descStr) bytes: the descriptive string descStr
    # - 132 bytes: 11 deciles, encoded as 11 * 3 floats (=min, max, count)
    # - array of n bytes, n = number of cells
    decChrList = [chr(x) for x in digArr]
    decStr = "".join(decChrList)
    geneIdLen = struct.pack("<H", len(geneDesc))

    binStr = binEncode(binInfo)
    geneStr = geneIdLen+geneDesc+binStr+decStr

    geneCompr = zlib.compress(geneStr)
    logging.debug("compression factor of %s: %f, before %d, after %d"% (geneDesc, float(len(geneCompr)) / len(geneStr), len(geneStr), len(geneCompr)))

    return geneCompr

def exprEncode(geneDesc, exprArr, matType):
    """ convert an array of numbers of type matType (int or float) to a compressed string of
    floats
    The format of a record is:
    - 2 bytes: length of descStr, e.g. gene identifier or else
    - len(descStr) bytes: the descriptive string descStr
    - array of n 4-byte floats (n = number of cells)
    """
    geneDesc = str(geneDesc) # make sure no unicode
    geneIdLen = struct.pack("<H", len(geneDesc))

    # on cortex-dev, numpy was around 30% faster. Not a huge difference.
    if numpyLoaded:
        exprStr = exprArr.tobytes()
    else:
        if matType=="float":
            arrType = "f"
        elif matType=="int":
            arrType = "I"
        else:
            assert(False) # internal error
        exprStr = array.array(arrType, exprArr).tostring()

    if isPy3:
        geneStr = geneIdLen+bytes(geneDesc, encoding="ascii")+exprStr
    else:
        geneStr = geneIdLen+geneDesc+exprStr

    geneCompr = zlib.compress(geneStr)

    fact = float(len(geneCompr)) / len(geneStr)
    logging.debug("raw - compression factor of %s: %f, before %d, after %d"% (geneDesc, fact, len(geneStr), len(geneCompr)))
    return geneCompr

def matrixToBin(fname, geneToSym, binFname, jsonFname, discretBinFname, discretJsonFname):
    """ convert gene expression vectors to vectors of deciles
        and make json gene symbol -> (file offset, line length)
    """
    logging.info("converting %s to %s and writing index to %s" % (fname, binFname, jsonFname))
    #logging.info("Shall expression values be log-transformed when transforming to deciles? -> %s" % (not skipLog))
    logging.info("Compressing gene expression vectors...")

    tmpFname = binFname + ".tmp"
    ofh = open(tmpFname, "wb")

    discretTmp = discretBinFname + ".tmp"
    discretOfh = open(discretTmp, "w")

    discretIndex = {}
    exprIndex = {}

    skipIds = 0
    highCount = 0

    logging.info("Auto-detecting number type of %s" % fname)
    geneIter = iterExprFromFile(fname, "float", None)
    matType = autoDetectMatType(geneIter, 10)
    logging.info("Numbers in matrix are of type '%s'", matType)

    geneIter = iterExprFromFile(fname, matType, geneToSym)

    geneCount = 0
    for geneId, sym, exprArr in geneIter:
        geneCount += 1

        #if maxVal(exprArr) > 200:
            #highCount += 1

        logging.debug("Processing %s, symbol %s" % (geneId, sym))
        exprStr = exprEncode(geneId, exprArr, matType)
        exprIndex[sym] = (ofh.tell(), len(exprStr))
        ofh.write(exprStr)

        #digArr, binInfo = digitizeArr(exprArr, matType)
        #discretStr = discretExprRowEncode(geneId, binInfo, digArr)

        #discretIndex[sym] = (discretOfh.tell(), len(discretStr))
        #discretOfh.write(discretStr)

        if geneCount % 1000 == 0:
            logging.info("Wrote expression values for %d genes" % geneCount)

    discretOfh.close()
    ofh.close()

    #if highCount==0:
        #logging.warn("No single value in the matrix is > 200. It looks like this matrix has been log'ed before. Our recommendation for visual inspection is to not transform matrices, but that is of course up to you.")
        #logging.error("Rerun with --skipLog.")
        #sys.exit(1)

    if len(exprIndex)==0:
        errAbort("No genes from the expression matrix could be mapped to symbols in the input file."
            "Are you sure these are Ensembl IDs? Adapt geneIdType in dataset.conf. Example ID: %s" % geneId)

    jsonOfh = open(jsonFname, "w")
    json.dump(exprIndex, jsonOfh)
    jsonOfh.close()

    jsonOfh = open(discretJsonFname, "w")
    json.dump(discretIndex, jsonOfh)
    jsonOfh.close()

    os.rename(tmpFname, binFname)
    os.rename(discretTmp, discretBinFname)

    return matType

def sepForFile(fname):
    if ".csv" in fname:
        return ","
    else:
        return "\t"

def indexMeta(fname, outFname):
    """ index a tsv by its first field. Writes binary data to outFname.
        binary data is (offset/4 bytes, line length/2 bytes)
    """
    ofh = open(outFname, "wb")
    logging.info("Indexing meta file %s to %s" % (fname, outFname))
    ifh = open(fname)
    sep = sepForFile(fname)
    headerDone = False
    for line, start, end in iterLineOffsets(ifh):
        if not headerDone:
            headerDone = True
            continue

        if isPy3:
            field1, _ = line.split(sep, maxsplit=1)
        else:
            field1, _ = string.split(line, sep, 1)

        lineLen = end - start
        assert(lineLen!=0)
        assert(lineLen<65535) # meta data line cannot be longer than 2 bytes
        ofh.write(struct.pack("<L", start))
        ofh.write(struct.pack("<H", lineLen))
    ofh.close()

def testMetaIndex(outDir):
    # test meta index
    fh = open(join(outDir, "meta.index"))
    #fh.seek(10*6)
    o = fh.read(4)
    s = fh.read(2)
    offset = struct.unpack("<L", o) # little endian
    l = struct.unpack("<H", s)
    #print "offset, linelen:", offset, l

    #fh = open(join(outDir, "meta/meta.tsv"))
    #fh.seek(offset[0])
    #print fh.read(l[0])

# ----------- main --------------

def parseColors(fname):
    " parse color table and return as dict value -> color "
    if not isfile(fname):
        logging.warn("File %s does not exist" % fname)
        return None

    colDict = parseDict(fname)
    newDict = {}
    for metaVal, color in colDict.iteritems():
        color = color.strip().strip("#") # hbeale had a file with trailing spaces
        assert(len(color)<=6) # colors can be no more than six hex digits
        for c in color:
            assert(c in "0123456789ABCDEFabcdef") # color must be a hex number
        newDict[metaVal] = color
    return newDict

def parseScaleCoordsAsDict(fname, useTwoBytes, flipY):
    """ parse tsv file in format cellId, x, y and return as dict (cellId, x, y)
    Flip the y coordinates to make it more look like plots in R, for people transitioning from R.
    """
    logging.info("Parsing coordinates from %s" % fname)
    coords = []
    maxY = 0
    minX = 2^32
    minY = 2^32
    maxX = -2^32
    maxY = -2^32
    skipCount = 0

    # parse and find the max values
    for row in lineFileNextRow(fname):
        assert(len(row)==3) # coord file has to have three rows (cellId, x, y), we just ignore the headers
        cellId = row[0]
        x = float(row[1])
        y = float(row[2])
        minX = min(x, minX)
        minY = min(y, minY)
        maxX = max(x, maxX)
        maxY = max(y, maxY)
        coords.append( (cellId, x, y) )

    if useTwoBytes:
        scaleX = 65535/(maxX-minX)
        scaleY = 65535/(maxY-minY)

    newCoords = {}
    for cellId, x, y in coords:
        if useTwoBytes:
            x = int(scaleX * (x - minX))
            y = int(scaleY * (y - minY))
            if flipY:
                y = 65535 - y
        else:
            if flipY:
                y = maxY - y

        newCoords[cellId] = (x, y)

    return newCoords

def metaReorder(matrixFname, metaFname, fixedMetaFname):
    """ check and reorder the meta data, has to be in the same order as the
    expression matrix, write to fixedMetaFname """

    logging.info("Checking and reordering meta data to %s" % fixedMetaFname)
    matrixSampleNames = readHeaders(matrixFname)[1:]
    metaSampleNames = readSampleNames(metaFname)

    # check that there is a 1:1 sampleName relationship
    mat = set(matrixSampleNames)
    meta = set(metaSampleNames)
    if len(meta)!=len(metaSampleNames):
        logging.error("sample names in the meta data differ in length from the sample names in the matrix: %d sample names in the meta data, %d sample names in the matrix" % (len(meta), len(metaSampleNames)))
        sys.exit(1)

    if len(mat.intersection(meta))==0:
        logging.error("Meta data and expression matrix have no single sample name in common. Sure that the expression matrix has one gene per row?")
        sys.exit(1)

    matNotMeta = meta - mat
    metaNotMat = mat - meta
    stop = False
    mustFilterMatrix = False
    if len(metaNotMat)!=0:
        logging.warn("%d samples names are in the meta data, but not in the expression matrix. Examples: %s" % (len(metaNotMat), list(metaNotMat)[:10]))
        logging.warn("These samples will be removed from the meta data")
        matrixSampleNames = [x for x in matrixSampleNames if x in meta]
        mustFilterMatrix = True

    if len(matNotMeta)!=0:
        logging.warn("%d samples names are in the expression matrix, but not in the meta data. Examples: %s" % (len(matNotMeta), list(matNotMeta)[:10]))
        logging.warn("These samples will be removed from the expression matrix")

    # filter the meta data file
    logging.info("Data contains %d samples/cells" % len(matrixSampleNames))

    # slurp in the whole meta data
    tmpFname = fixedMetaFname+".tmp"
    ofh = open(tmpFname, "w")
    metaToLine = {}
    for lNo, line in enumerate(open(metaFname)):
        if lNo==0:
            ofh.write(line)
            continue
        row = line.rstrip("\n").rstrip("\r").split("\t")
        metaToLine[row[0]] = line

    # and write it in the right order
    for matrixName in matrixSampleNames:
        ofh.write(metaToLine[matrixName])
    ofh.close()
    os.rename(tmpFname, fixedMetaFname)

    return matrixSampleNames, mustFilterMatrix

def writeCoords(coords, sampleNames, coordBinFname, coordJson, useTwoBytes, coordInfo):
    """ write coordinates given as a dictionary to coordBin and coordJson, in the order of sampleNames
    Also return as a list.
    """
    tmpFname = coordBinFname+".tmp"
    logging.info("Writing coordinates to %s and %s" % (coordBinFname, coordJson))
    binFh = open(tmpFname, "wb")

    minX = 2^32
    minY = 2^32
    maxX = -2^32
    maxY = -2^32
    xVals = []
    yVals = []

    #print coords['Hi_GW21_4.Hi_GW21_4']
    #print len(sampleNames)
    for sampleName in sampleNames:
        coordTuple = coords.get(sampleName)
        if coordTuple is None:
            logging.warn("sample name %s is in meta file but not in coordinate file" % sampleName)
            x = 0
            y = 0
        else:
            x, y = coordTuple
        minX = min(x, minX)
        minY = min(y, minY)
        maxX = max(x, maxX)
        maxY = max(y, maxY)

        # all little endian
        if useTwoBytes:
            binFh.write(struct.pack("<H", x))
            binFh.write(struct.pack("<H", y))
        else:
            binFh.write(struct.pack("<f", x))
            binFh.write(struct.pack("<f", y))

        xVals.append(x)
        yVals.append(y)
    
    binFh.close()
    os.rename(tmpFname, coordBinFname)

    coordInfo["minX"] = minX
    coordInfo["maxX"] = maxX
    coordInfo["minY"] = minY
    coordInfo["maxY"] = maxY
    if useTwoBytes:
        coordInfo["type"] = "Uint16"
    else:
        coordInfo["type"] = "Float32"

    logging.info("Wrote %d coordinates to %s" % (len(sampleNames), coordBinFname))
    return coordInfo, xVals, yVals

def runCommand(cmd):
    " run command "
    logging.info("Running %s" % cmd)
    err = os.system(cmd)
    if err!=0:
        errAbort("Could not run: %s" % cmd)
    return 0

def copyMatrix(inFname, outFname, filtSampleNames, doFilter):
    " copy matrix and compress it. If doFilter is true: keep only the samples in filtSampleNames"
    if not doFilter:
        logging.info("Copying %s to %s" % (inFname, outFname))
        #shutil.copy(inFname, outFname)
        cmd = "cp %s %s" % (inFname, outFname)
        ret = runCommand(cmd)
        if ret!=0 and isfile(outFname):
            os.remove(outFname)
            sys.exit(1)

        return

    sep = "\t"

    logging.info("Copying %s to %s, keeping only the %d columns with a sample name in the meta data" % (inFname, outFname, len(filtSampleNames)))

    ifh = openFile(inFname)

    headLine = ifh.readline()
    headers = headLine.rstrip("\n").rstrip("\r").split(sep)

    keepFields = set(filtSampleNames)
    keepIdx = [0]
    for i, name in enumerate(headers):
        if name in keepFields:
            keepIdx.append(i)

    tmpFname = outFname+".tmp"
    ofh = openFile(tmpFname, "w")
    ofh.write("\t".join(filtSampleNames))
    ofh.write("\n")

    for line in ifh:
        row = line.rstrip("\n").rstrip("\r").split(sep)
        newRow = []
        for idx in keepIdx:
            newRow.append(row[idx])
        ofh.write("\t".join(newRow))
        ofh.write("\n")
    ofh.close()

    os.rename(tmpFname, outFname)

def convIdToSym(geneToSym, geneId):
    if geneToSym is None:
        return geneId
    else:
        return geneToSym[geneId]

def splitMarkerTable(filename, geneToSym, outDir):
    " split .tsv on first field and create many files in outDir with the non-first columns. Also map geneIds to symbols. "
    if filename is None:
        return
    logging.info("Splitting cluster markers from %s into directory %s" % (filename, outDir))
    #logging.debug("Splitting %s on first field" % filename)
    ifh = openFile(filename)

    headers = ifh.readline().rstrip("\n").split('\t')
    otherHeaders = headers[2:]

    data = defaultdict(list)
    for line in ifh:
        row = line.rstrip("\n").split('\t')
        clusterName = row[0]
        geneId = row[1]
        scoreVal = float(row[2])
        otherFields = row[3:]

        #geneSym = convIdToSym(geneToSym, geneId)
        geneSym = geneId # let's assume for now that the marker table already has symbols

        newRow = []
        newRow.append(geneId)
        newRow.append(geneSym)
        newRow.append(scoreVal)
        newRow.extend(otherFields)

        data[clusterName].append(newRow)

    newHeaders = ["id", "symbol"]
    newHeaders.extend(otherHeaders)

    fileCount = 0
    for clusterName, rows in iteritems(data):
        #rows.sort(key=operator.itemgetter(2), reverse=True) # rev-sort by score (fold change)
        clusterName = clusterName.replace("/","_")
        outFname = join(outDir, clusterName+".tsv")
        logging.debug("Writing %s" % outFname)
        ofh = open(outFname, "w")
        ofh.write("\t".join(newHeaders))
        ofh.write("\n")
        for row in rows:
            row[2] = "%0.3f" % row[2] # limit to 3 digits
            ofh.write("\t".join(row))
            ofh.write("\n")
        ofh.close()
        fileCount += 1
    logging.info("Wrote %d .tsv files into directory %s" % (fileCount, outDir))

def execfile(filepath, globals=None, locals=None):
    " version of execfile for both py2 and py3 "
    if globals is None:
        globals = {}
    globals.update({
        "__file__": filepath,
        "__name__": "__main__",
    })
    with open(filepath, 'rb') as file:
        exec(compile(file.read(), filepath, 'exec'), globals, locals)

def loadConfig(fname):
    " parse python in fname and return variables as dictionary "
    g = {}
    l = OrderedDict()
    execfile(fname, g, l)

    conf = l

    if not "coords" in conf:
        errAbort("The input configuration has to define the 'coords' statement")
    if not "meta" in conf:
        errAbort("The input configuration has to define the 'meta' statement")
    if not "exprMatrix" in conf:
        errAbort("The input configuration has to define the 'exprMatrix' statement")

    return conf

def guessConfig(options):
    " guess reasonable config options from arguments "
    conf = {}
    conf.name = dirname(options.matrix)

    #if options.inDir:
        #inDir = options.inDir
        #metaFname = join(inDir, "meta.tsv")
        #matrixFname = join(inDir, "exprMatrix.tsv")
        #coordFnames = [join(inDir, "tsne.coords.tsv")]
        #markerFname = join(inDir, "markers.tsv")
        #if isfile(markerFname):
            #markerFnames = [markerFname]
        #else:
            #markerFnames = None
#
        #acronymFname = join(inDir, "acronyms.tsv")
        #if isfile(acronymFname):
            #otherFiles["acronyms"] = [acronymFname]
#
        #markerFname = join(inDir, "markers.tsv")
        #if isfile(acronymFname):
            #otherFiles["markerLists"] = [markerFname]
    return conf

def copyHtmlFiles(inDir, conf):
    " copy description html files to output directory "
    copyFiles = []

    conf["desc"] = {}

    fname = makeAbs(inDir, "summary.html")
    if not isfile(fname):
        logging.warn("%s does not exist" % fname)
    else:
        copyFiles.append( (fname, "summary.html") )
        conf["desc"]["summary"] = "summary.html"

    fname = makeAbs(inDir, "methods.html")
    if not isfile(fname):
        logging.warn("%s does not exist" % fname)
    else:
        copyFiles.append( (fname, "methods.html") )
        conf["desc"]["methods"] = "methods.html"

    fname = makeAbs(inDir, "downloads.html")
    if not isfile(fname):
        logging.warn("%s does not exist" % fname)
    else:
        copyFiles.append( (fname, "downloads.html") )
        conf["desc"]["downloads"] = "downloads.html"

    fname = makeAbs(inDir, "thumb.png")
    if not isfile(fname):
        logging.warn("%s does not exist" % fname)
    else:
        copyFiles.append( (fname, "thumb.png") )
        conf["desc"]["thumbnail"] = "thumb.png"

    return conf, copyFiles

def findInputFiles(options):
    """ find all input files and return them
    returns these:
    metaFname = file name meta data
    matrixFname = file name expression matrix
    coordFnames = list of (filename, label)
    markerFnames = list of (filename, label)
    filesToCopy = list of (origFname, copyToFname)
    """
    if options.inConf:
        conf = loadConfig(options.inConf)
        inDir = dirname(options.inConf)
    else:
        conf = guessConfig(options)

    conf, copyFiles = copyHtmlFiles(inDir, conf)

    return inDir, conf, copyFiles

def makeAbs(inDir, fname):
    " return absolute path of fname under inDir "
    return abspath(join(inDir, fname))

def makeAbsDict(inDir, dicts):
    " given list of dicts with key 'file', make paths absolute "
    for d in dicts:
        d["file"] = makeAbs(inDir, d["file"])
    return dicts

def parseTsvColumn(fname, colName):
    " parse a tsv file and return column as a pair (values, assignment row -> index in values) "
    logging.info("Parsing column %s from %s" % (colName, fname))
    vals = parseOneColumn(fname, colName)

    newVals = []
    valToInt = {}
    maxIdx = -1
    for v in vals:
        if v not in valToInt:
            maxIdx+=1
            valToInt[v] = maxIdx
        idx = valToInt[v]
        newVals.append(idx)


    # inverse key/val dict
    intToVal = {}
    for k, v in iteritems(valToInt):
        intToVal[v] = k

    valArr = []
    for i in range(0, maxIdx+1):
        valArr.append(intToVal[i])

    return newVals, valArr

def makeMids(xVals, yVals, labelVec, labelVals, coordInfo):
    """
    calculate the positions (centers) for the cluster labels
    given a coord list and a vector of the same size with the label indices, return a list of [x, y, coordLabel]
    """
    logging.info("Calculating cluster midpoints")
    assert(len(xVals)==len(labelVec)==len(yVals))

    # prep the arrays
    clusterXVals = []
    clusterYVals = []
    for i in range(len(labelVals)):
        clusterXVals.append([])
        clusterYVals.append([])
    assert(len(clusterXVals)==len(labelVals))

    # sort the coords into separate arrays, one per cluster
    for i in range(len(labelVec)):
        #for (x, y), clusterIdx in zip(coords, labelVec):
        clusterIdx = labelVec[i]
        clusterXVals[clusterIdx].append(xVals[i])
        clusterYVals[clusterIdx].append(yVals[i])

    midInfo = []
    for clustIdx, xList in enumerate(clusterXVals):
        yList = clusterYVals[clustIdx]
        # get the midpoint of this cluster
        midX = sum(xList) / float(len(xList))
        midY = sum(yList) / float(len(yList))

        # take only the best 70% of the points closest to the midpoints
        xyDist = []
        for x, y in zip(xList, yList):
            dist = math.sqrt((x-midX)**2+(y-midY)**2)
            xyDist.append( (dist, x, y) )
        xyDist.sort()
        xyDistBest = xyDist[:int(0.7*len(xyDist))]

        # now recalc the midpoint
        xSum = sum([x for dist, x, y in xyDistBest])
        ySum = sum([y for dist, x, y in xyDistBest])
        fixMidX = xSum / float(len(xyDistBest))
        fixMidY = ySum / float(len(xyDistBest))

        clusterName = labelVals[clustIdx]
        midInfo.append([fixMidX, fixMidY, clusterName])

    # make some minimal effort to reduce overlaps
    #spanX = coordInfo['maxX'] - coordInfo['minX']
    #spanY = coordInfo['maxY'] - coordInfo['minY']
    #tickX = spanX / 1000 # rough guess how much one pixel could be on 
    #tickY = spanY / 1000 # the final screen
    #for i, (midX1, midY1, clusterName1) in enumerate(midInfo):
        #print "first", i, midX1, midY1, clusterName1
        #for j, (midX2, midY2, clusterName2) in enumerate(midInfo[i+1:]):
            #print "second", j, midX2, midY2, clusterName1, clusterName2
            #distX = abs(midX2-midX1)
            #distY = abs(midY2-midY1)
            #print distX, distY
            ## if distance between two labels too short:
            #dist = math.sqrt((((midX2-midX1)/tickX)**2+((midY2-midY1)/tickY)**2))
            #print "dist in pixels", dist
            #if dist< 30:
                #print "moving"
                #print "before", midInfo[j]
                ## move the first label slightly downwards
                #midInfo[j][1] = midY1 + 5 * tickY
                #print "after", midInfo[j]

    return midInfo

def readHeaders(fname):
    " return headers of a file "
    logging.info("Reading headers of file %s" % fname)
    ifh = openFile(fname, "rt")
    line1 = ifh.readline().rstrip("\n").rstrip("\r")
    sep = sepForFile(fname)
    return line1.split(sep)

def parseGeneInfo(geneToSym, fname):
    """ parse a file with three columns: symbol, desc (optional), pmid (optional).
    Return as a dict symbol -> [description, pmid] """
    if fname is None:
        return {}
    logging.info("Parsing %s" % fname)
    validSyms = None
    if geneToSym is not None:
        validSyms = set()
        for gene, sym in geneToSym.iteritems():
            validSyms.add(sym)

    geneInfo = []
    hasDesc = None
    hasPmid = None
    for row in lineFileNextRow(fname):
        if hasDesc == None:
            if "desc" in row._fields:
                hasDesc = True
        if hasPmid == None:
            if "pmid" in row._fields:
                hasPmid = True
        sym = row.symbol
        if validSyms is not None and sym not in validSyms:
            logging.error("'%s' is not a valid gene gene symbol, skipping it" % sym)
            continue

        info = [sym]
        if hasDesc:
            info.append(row.desc)
        if hasPmid:
            info.append(row.pmid)
        geneInfo.append(info)
    return geneInfo

def readSampleNames(fname):
    " read only the first column of fname, strip the headers "
    logging.info("Reading sample names from %s" % fname)
    sampleNames = []
    i = 1
    doneNames = set()
    for row in lineFileNextRow(fname):
        metaName = row[0]
        if metaName=="":
            logging.error("invalid sample name - line %d in %s: sample name (first field) is empty" % (i, fname))
            sys.exit(1)
        if metaName in doneNames:
            logging.error("sample name duplicated - line %d in %s: sample name %s (first field) has been seen before" % (i, fname, metaName))
            sys.exit(1)

        doneNames.add(metaName)
        sampleNames.append(row[0])
        i+=1
    return sampleNames

def addDataset(inDir, conf, fileToCopy, outDir, options):
    " write config to outDir and copy over all files in fileToCopy "
    # keep a copy of the original config in the output directory for debugging later
    confName = join(outDir, "origConf.json")
    json.dump(conf, open(confName, "w"))

    for src, dest in fileToCopy:
        outPath = join(outDir, dest)
        logging.info("Copying %s -> %s" % (src, outPath))
        shutil.copy(src, outPath)

    matrixFname = makeAbs(inDir, conf["exprMatrix"])
    metaFname = makeAbs(inDir, conf["meta"])
    coordFnames = makeAbsDict(inDir, conf["coords"])
    markerFnames = makeAbsDict(inDir, conf["markers"])
    descJsonFname = join(outDir, "dataset.json")

    colorFname = conf.get("colors")
    enumFields = conf.get("enumFields")

    geneIdType = conf.get("geneIdType")
    if geneIdType==None:
        errAbort("geneIdType must have a value in dataset.conf")

    if geneIdType == 'symbols' or geneIdType=="symbol":
        geneToSym = None
    else:
        searchMask = join(dataDir, geneIdType+".*.tab")
        fnames = glob.glob(searchMask)
        assert(len(fnames)==1)
        geneIdTable = fnames[0]
        geneToSym = readGeneToSym(geneIdTable)

    quickGeneFname = conf.get("quickGenesFile")
    if quickGeneFname:
        fname = makeAbs(inDir, quickGeneFname)
        quickGenes = parseGeneInfo(geneToSym, fname)
        del conf["quickGenesFile"]
        conf["quickGenes"] = quickGenes
        logging.info("Read %d quick genes from %s" % (len(quickGenes), fname))

    # these don't exist / are not needed in the output json file
    del conf["meta"]
    del conf["exprMatrix"]
    del conf["colors"]
    if "enumFields" in conf:
        del conf["enumFields"]
    if "tags" in conf and type(conf["tags"])!=type([]):
        errAbort("'tags' in config file must be a list")

    # in quick mode, reuse the old config file
    if options.quick and isfile(descJsonFname):
        logging.info("Quick-mode: re-using config file %s" % descJsonFname)
        oldConf = json.load(open(descJsonFname))
        for k, v in oldConf.iteritems():
            if k not in conf:
                conf[k] = v

    # convert the meta data to binary
    metaDir = join(outDir, "metaFields")
    makeDir(metaDir)
    metaIdxFname = join(outDir, "meta.index")

    finalMetaFname = join(outDir, "meta.tsv")
    if isfile(metaIdxFname) and options.quick:
        logging.info("quick-mode: %s already exists, not recreating" % metaIdxFname)
        sampleNames = readSampleNames(finalMetaFname)
        needFilterMatrix = conf["matrixWasFiltered"]
    else:
        # create a meta file for downloads, same order as expression matrix
        sampleNames, needFilterMatrix = metaReorder(matrixFname, metaFname, finalMetaFname)
        conf["sampleCount"] = len(sampleNames)
        conf["matrixWasFiltered"] = needFilterMatrix
        conf = metaToBin(finalMetaFname, colorFname, metaDir, enumFields, conf)
        indexMeta(finalMetaFname, metaIdxFname)
    logging.info("Kept %d cells present in both meta data file and expression matrix" % len(sampleNames))

    writeJson(conf, descJsonFname)
    # process the expression matrix: two steps

    myMatrixFname = join(outDir, "exprMatrix.tsv.gz")

    binMat = join(outDir, "exprMatrix.bin")
    binMatIndex = join(outDir, "exprMatrix.json")
    discretBinMat = join(outDir, "discretMat.bin")
    discretMatrixIndex = join(outDir, "discretMat.json")

    if options.quick and isfile(myMatrixFname):
        logging.info("quick-mode: Not copying+reordering expression matrix %s" % myMatrixFname)
    else:
        nozipFname = join(outDir, "exprMatrix.tsv")
        copyMatrix(matrixFname, nozipFname, sampleNames, needFilterMatrix)
        runCommand("gzip -f %s" % nozipFname)

    # step1: discretize expression matrix for the viewer
    if options.quick and isfile(binMat):
        logging.info("quick-mode: Not compressing matrix, because %s already exists" % binMat)
    else:
        matType = matrixToBin(myMatrixFname, geneToSym, binMat, binMatIndex, discretBinMat, discretMatrixIndex)
        if matType=="int":
            conf["matrixArrType"] = "Uint32"
        elif matType=="float":
            conf["matrixArrType"] = "Float32"

    # step2: copy expression matrix, so people can download (potentially removing the sample names missing from the meta data)
    # convert the coordinates
    useTwoBytes = conf.get("useTwoBytes", False)
    newCoords = []
    for coordIdx, coordInfo in enumerate(coordFnames):
        coordFname = coordInfo["file"]
        coordLabel = coordInfo["shortLabel"]
        coords = parseScaleCoordsAsDict(coordFname, useTwoBytes, True)
        coordName = "coords_%d" % coordIdx
        coordDir = join(outDir, "coords", coordName)
        makeDir(coordDir)
        coordBin = join(coordDir, "coords.bin")
        coordJson = join(coordDir, "coords.json")
        coordInfo = OrderedDict()
        coordInfo["name"] = coordName
        coordInfo["shortLabel"] = coordLabel
        coordInfo, xVals, yVals = writeCoords(coords, sampleNames, coordBin, coordJson, useTwoBytes, coordInfo)
        newCoords.append( coordInfo )
        conf["coords"] = newCoords

        if "labelField" in conf:
            clusterLabelField = conf["labelField"]
            labelVec, labelVals = parseTsvColumn(finalMetaFname, clusterLabelField)
            clusterMids = makeMids(xVals, yVals, labelVec, labelVals, coordInfo)

            midFname = join(coordDir, "clusterLabels.json")
            midFh = open(midFname, "w")
            json.dump(clusterMids, midFh, indent=2)
            logging.info("Wrote cluster labels and midpoints to %s" % midFname)

    # save the acronyms
    fname = conf.get("acroFname")
    if fname is not None:
        fname = makeAbs(inDir, fname)
        if not isfile(fname):
            logging.warn("%s specified in config file, but does not exist, skipping" % fname)
        else:
            acronyms = parseDict(fname)
            logging.info("Read %d acronyms from %s" % (len(acronyms), fname))
            conf["acronyms"] = acronyms
        del conf["acroFname"]

    # convert the markers
    newMarkers = []
    for markerIdx, markerInfo in enumerate(markerFnames):
        markerFname = markerInfo["file"]
        markerLabel = markerInfo["shortLabel"]

        clusterName = "markers_%d" % markerIdx # use sha1 of input file ?
        markerDir = join(outDir, "markers", clusterName)
        makeDir(markerDir)

        splitMarkerTable(markerFname, geneToSym, markerDir)

        newMarkers.append( {"name" : clusterName, "shortLabel" : markerLabel})
    conf["markers"] = newMarkers

    writeJson(conf, descJsonFname)

def scanpyToTsv(anndata, path , meta_option=None, nb_marker=100):
    """
    Written by Lucas Seninge, lucas.seninge@etu.unistra.fr

    Given a scanpy object, write dataset to output directory under path
    
    This function export files needed for the ucsc cells viewer from the Scanpy Anndata object
    :param anndata: Scanpy AnnData object where information are stored
    :param path : Path to folder where to save data (tsv tables)
    :param meta_option: list of metadata names (string) present
    in the AnnData objects(other than 'louvain' to also save (eg: batches, ...))
    :param nb_marker: number of cluster markers to store. Default: 100
    
    """
    import numpy as np
    import pandas as pd
    import scanpy.api as sc

    ##Save data matrix to tsv
    #if "raw" in dir(anndata):
    #    adT = anndata.raw.T
    #else:
    adT = anndata.T

    data_matrix=pd.DataFrame(adT.X, index=adT.obs.index.tolist(), columns=adT.var.index.tolist())
    data_matrix.to_csv(join(path, 'exprMatrix.tsv'),sep='\t',index=True)

    ##Check for tsne coord
    if 'X_tsne' in anndata.obsm.dtype.names:
        #Export tsne coord 
        tsne_coord=pd.DataFrame(anndata.obsm.X_tsne,index=anndata.obs.index)
        tsne_coord.columns=['tsne_1','tsne_2']
        fname = join(path, "tsne_coords.tsv")
        tsne_coord.to_csv(fname,sep='\t')
    else:
        errAbort('Couldnt find T-SNE coordinates')
    
    ##Check for umap coord
    if 'X_umap' in anndata.obsm.dtype.names:
        #Export umap coord 
        umap_coord=pd.DataFrame(anndata.obsm.X_umap,index=anndata.obs.index)
        umap_coord.columns=['umap_1','umap_2']
        fname = join(path, "umap_coords.tsv")
        umap_coord.to_csv(fname,sep='\t')
    else:
        errAbort('Couldnt find UMAP coordinates')

    ##Check for louvain clustering
    if 'louvain' in anndata.obs:
        #Export cell <-> cluster identity
        fname = join(path, 'cell_to_cluster.tsv')
        anndata.obs[['louvain']].to_csv(fname,sep='\t')
    else:
        errAbort('Couldnt find clustering information')

    ##Check for cluster markers
    if 'rank_genes_groups' in anndata.uns:
        top_score=pd.DataFrame(anndata.uns['rank_genes_groups']['scores']).loc[:nb_marker]
        top_gene=pd.DataFrame(anndata.uns['rank_genes_groups']['names']).loc[:nb_marker]
        marker_df= pd.DataFrame()
        for i in range(len(top_score.columns)):
            concat=pd.concat([top_score[[str(i)]],top_gene[[str(i)]]],axis=1,ignore_index=True)
            concat['cluster_number']=i
            col=list(concat.columns)
            col[0],col[-2]='z_score','gene'
            concat.columns=col
            marker_df=marker_df.append(concat)
    else:
        errAbort ('Couldnt find cluster markers list')

    #Rearranging columns -> Cluster, gene, score
    cols=marker_df.columns.tolist()
    cols=cols[::-1]
    marker_df=marker_df[cols]
    #Export
    fname = join(path, "markers.tsv")
    pd.DataFrame.to_csv(marker_df,fname,sep='\t',index=False)

    ##Save more metadata
    if meta_option != None:
        meta_df=pd.DataFrame()
        for element in meta_option:
            if element not in anndata.obs:
                print(str(element) + ' field is not present in the AnnData object')
            else:
                temp=anndata.obs[[element]]
                meta_df=pd.concat([meta_df,temp],axis=1)
        fname = join(path, "meta.tsv")
        meta_df.to_csv(fname,sep='\t')

def writeJson(conf, descJsonFname):
    # write dataset summary info
    descJsonFh = open(descJsonFname, "w")
    json.dump(conf, descJsonFh, indent=2)
    logging.info("Wrote %s" % descJsonFname)

def main():
    args, options = parseArgs()

    if options.test:
        doctest.testmod()
        sys.exit(0)

    if not isfile(options.inConf):
        errAbort("File %s does not exist." % options.inConf, showHelp=True)
    if options.outDir is None:
        errAbort("You have to specify at least the output directory.", showHelp=True)

    inDir, conf, filesToCopy = findInputFiles(options)

    outDir = options.outDir

    if outDir is None:
        errAbort("You have to specify at least the output directory or set the env. variable CBOUT.")

    datasetDir = join(outDir, conf["name"])
    makeDir(datasetDir)

    addDataset(inDir, conf, filesToCopy, datasetDir, options)

if __name__=="__main__":
    #main()
    import scanpy.api as sc
    ad = sc.read("sampleData/quakeBrainGeo1.old/geneMatrix.tsv")
    ad = ad.T
    convScanpy(ad, "temp", "./")
