################################################################################
#
#   MRC FGU Computational Genomics Group
#
#   $Id: pipeline_snps.py 2870 2010-03-03 10:20:29Z andreas $
#
#   Copyright (C) 2009 Andreas Heger
#
#   This program is free software; you can redistribute it and/or
#   modify it under the terms of the GNU General Public License
#   as published by the Free Software Foundation; either version 2
#   of the License, or (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program; if not, write to the Free Software
#   Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.
#################################################################################
"""

:Author: Andreas Heger
:Release: $Id: pipeline_snps.py 2870 2010-03-03 10:20:29Z andreas $
:Date: |today|
:Tags: Python

Purpose
-------

snp annotation pipeline.

Input:

Indels in pileup format.

Usage
-----

Type::

   python <script_name>.py --help

for command line help.

Code
----

"""
from ruffus import *
import sys, glob, gzip, os, itertools, CSV, re, math, types, collections
import optparse, shutil
import sqlite3
import GFF, GTF
import Experiment as E
import Pipeline as P
import IOTools
import Genomics
import Database
import FastaIterator
import PipelineGeneset as PGeneset
import PipelineEnrichment as PEnrichment
import PipelineGO as PGO
import scipy.stats
import Stats
import pysam

from rpy import r as R
import rpy

###################################################################
###################################################################
###################################################################
## read global options from configuration file
P.getParameters( 
    ["%s.ini" % __file__[:-len(".py")],
     "../pipeline.ini",
     "pipeline.ini" ] )

P.PARAMS.update( 
    { "transcripts" :"transcripts.gtf.gz",
      "genes" : 'genes.gtf.gz',
      "annotation" : 'geneset_regions.gff.gz',
      "peptides": 'peptides.fasta',
      "cdna": 'cdna.fasta',
      "cds": 'cds.fasta' } )

PARAMS = P.PARAMS

if not os.path.exists("conf.py"):
    raise IOError( "could not find configuration file conf.py" )

execfile("conf.py")
SEPARATOR = "|"

###################################################################
###################################################################
###################################################################
## import targets - these are targets outside the main pipeline and
## are mostly utilities to parse various data sources into the format
## used by the pipeline.
###################################################################
###################################################################
###################################################################


###################################################################
###################################################################
###################################################################
## import variants
###################################################################
if PARAMS["filename_snps"]:
    @split( PARAMS["filename_snps"], "*.pileup.gz" )
    def importSNPs( infile, outfile ):
        '''build samtools pileup formatted files from tabular output
        #CHROM  POS     REF     129P2   129S1   129S5   AKR     A_J     BALB    C3H     C57BL   CAST    CBA     DBA     LP_J    NOD     NZO     PWK     SPRET   WSB
        '''

        outfiles = IOTools.FilePool( "mouse%s.pileup" )

        inf = gzip.open(infile,"r")
        headers = []
        counts = E.Counter()

        if "filename_refseq_filter" in PARAMS:
            intervals = GTF.readAndIndex( GTF.iterator( IOTools.openFile(PARAMS["filename_refseq_filter"], "r") ) )
        else:
            intervals = None
        
        for line in inf:
            data = line[:-1].split("\t")
            if line.startswith("#"):
                if not headers: headers = data[3:]
                continue

            counts.input += 1

            contig, pos, ref = data[:3]
            pos = int(pos)

            if intervals:
                if not intervals.contains( "chr%s" % contig, pos-1, pos ):
                    counts.filter += 1
                    continue

            for h, genotype in zip(headers, data[3:]):
                if genotype == "..": continue
                outfiles.write( h, 
                                "\t".join( map(str, (
                                contig,
                                pos,
                                ref,
                                Genomics.encodeGenotype( genotype ),
                                "0",
                                "0",
                                "0",
                                "0",
                                genotype,
                                "<" * len(genotype) ) ) ) + "\n" )
                
                counts.output += 1

        outfiles.close()

        E.info("%s" % str(counts) )

        E.info("starting compression and indexing" )

        # convert to bgzip and index with tabix
        outfiles = glob.glob( "mouse*.pileup" )
        
        for outfile in outfiles:
            E.info("compressing and indexing %s" % outfile )
            pysam.tabix_index( outfile, preset = "vcf" )

elif PARAMS["filename_pileup"]:
    pass

elif PARAMS["filename_vcf"]:
    @split( PARAMS["filename_vcf"], "*.pileup.gz" )
    def importSNPs( infile, outfile ):
        '''build samtools pileup formatted files from vcf formatted files.

        The column to strain mapping are determined dynamically.

        #CHROM  POS     ID      REF     ALT     QUAL    FILTER  INFO    FORMAT  129P2   129S1   129S5   AKR     A_J     BALB    C3H     C57BL   CAST    CBA     DBA     LP_J    NOD     NZO     PWK     SPRET   WSB
        #CHROM  POS     ID      REF     ALT     QUAL    FILTER  INFO    FORMAT  129P2   129S5   AKR     C3H     CAST    DBA     NOD     PWK     WSB     129S1   A_J     BALB    C57BL   CBA     LP_J    NZO     SPRET

        The parser is incomplete (only looks at unphased data, etc.)

        IT ALSO IGNORES HETEROZYGOUS CALLS.

        Both vcf and pileup employ 1-based coordinate systems.
        '''

        outfiles = IOTools.FilePool( "mouse%s.pileup.gz" )

        inf = gzip.open(infile,"r")
        headers = []
        ninput = 0
        counts = E.Counter()
        for line in inf:
            data = line[:-1].split("\t")
            if line.startswith("#CHROM"):
                if not headers: headers = data[9:]
                continue
            elif line.startswith("#"):
                continue

            contig, pos, ref = data[0], data[1], data[3]

            # if contig != "19": continue

            pos = int(pos)
            variants = [ref]
            variants.extend( data[4].split(",") )
            counts.input += 1

            for h, genotype_info in zip(headers, data[9:]):
                # no variant for this strain - skip
                if genotype_info == "." or genotype_info.startswith("./."): continue

                # determine the genotype base
                genotype = genotype_info.split(":")[0].split("/")

                # ignore heterozygous calls
                if len(set(genotype)) != 1: continue

                genotype = [ variants[int(x)] for x in genotype ]
                lengths = [len(x) for x in genotype] + [len(ref)]
                is_snp = len( set(lengths) ) == 1 and lengths[0] == 1

                # skip genotypes for which no call can be made
                if "." in genotype: continue

                if is_snp:
                    genotype = "".join(genotype)

                    # skip wild type 
                    if genotype == "%s%s" % (ref,ref):
                        continue

                    outfiles.write( h, 
                                "\t".join( map(str, (
                                contig,
                                pos,
                                ref,
                                Genomics.encodeGenotype( genotype ),
                                "0",
                                "0",
                                "0",
                                "0",
                                genotype,
                                "<" * len(genotype) ) ) ) + "\n" )
                else:

                    def getPrefix( s1, s2 ):
                        '''get common prefix of strings s1 and s2.'''
                        n = min( len( s1), len( s2 ) )
                        predix = []
                        for x in range( n ):
                            if s1[x] != s2[x]: return s1[:x]
                        return s1[:n]

                    def getSuffix( s1, s2 ):
                        '''get common sufix of strings s1 and s2.'''
                        n = min( len( s1), len( s2 ) )
                        predix = []
                        if s1[-1] != s2[-1]: return ""
                        for x in range( -2, -n - 1, -1 ):
                            if s1[x] != s2[x]: return s1[x+1:]
                        return s1[-n:]

                    def getGenotype( variant, ref ):
                        if variant == ref: return "*", 0

                        if len(ref) > len(variant):
                            # is a deletion
                            if ref.startswith(variant):
                                return "-%s" % ref[len(variant):], len(variant) - 1
                            elif ref.endswith( variant ):
                                return "-%s" % ref[:-len(variant)], -1
                            else:
                                prefix = getPrefix( ref, variant )
                                suffix = getSuffix( ref, variant )
                                shared = len(prefix) + len(suffix) - len(variant) 
                                # print "-", prefix, suffix, ref, variant, shared, len(prefix), len(suffix), len(ref)
                                if shared < 0:
                                    raise ValueError()
                                return "-%s" % ref[len(prefix):-(len(suffix)-shared)], len(prefix) - 1

                        elif len(ref) < len(variant):
                            # is an insertion
                            if variant.startswith(ref):
                                return "+%s" % variant[len(ref):], len(ref) - 1
                            elif variant.endswith(ref):
                                return "+%s" % variant[:len(ref)], 0
                            else:
                                prefix = getPrefix( ref, variant )
                                suffix = getSuffix( ref, variant )
                                shared = len(prefix) + len(suffix) - len(ref) 
                                if shared < 0:
                                    raise ValueError()

                                return "+%s" % variant[len(prefix):-(len(suffix)-shared)], len(prefix)
                        else:
                            assert 0, "snp?"

                    # in pileup, the position refers to the base
                    # after the coordinate, hence subtract 1
                            #pos -= 1

                    genotypes, offsets = [], []
                    is_error = True
                    for variant in genotype:
                        try:
                            g, offset = getGenotype( variant, ref ) 
                        except ValueError:
                            break

                        assert len(g) > 1, "incomplete genotype %s" % g
                        genotypes.append( g )
                        offsets.append( offset )
                    else: 
                        is_error = False
                    if is_error: 
                        print line,
                        counts.errors += 1
                        continue

                    assert len(set(offsets )) == 1
                    offset = offsets[0]

                    genotypes = "/".join( genotypes )
                    outfiles.write( h, 
                                "\t".join( map(str, (
                                contig,
                                pos + offset,
                                "*",
                                genotypes,
                                "0",
                                "0",
                                "0",
                                "0",
                                genotypes,
                                "<" * len(genotype),
                                "0", 
                                "0",
                                "0") ) ) + "\n" )

        outfiles.close()
        E.info("%s" % str(counts))

        for outfile in outfiles:
            # need to sort as overlapping indels might not be in correct
            # order even if input was sorted.
            E.info("sorting %s" % outfile )
            statement = "gunzip < %(outfile)s | sort -k1,1 -k2,2n | bgzip > %(outfile)s.tmp; mv %(outfile)s.tmp %(outfile)s"
            P.run()

            E.info("compressing and indexing %s" % outfile )
            pysam.tabix_index( outfile, preset = "vcf" )
    
    ###################################################################
    ###################################################################
    ###################################################################
    @jobs_limit(2)
    @transform( "*.pileup.gz", suffix(".pileup.gz"), "_pileup.load")
    def loadPileup( infile, outfile ):
        '''load pileup information.

        only loads chromosome, pos, genotype (first three columns)
        '''

        to_cluster = False
        tablename = outfile[:-len(".load")]
        statement = '''
        gunzip < %(infile)s |
        awk 'BEGIN {printf("contig\\tpos\\treference\\tgenotype\\n")} 
                   {printf("%%s\\t%%s\\t%%s\\t%%s\\n",$1,$2,$3,$4)}' |
            csv2db.py %(csv2db_options)s \
            --index=contig,pos \
            --table=%(tablename)s
        > %(outfile)s
        '''

        P.run()

###################################################################
###################################################################
###################################################################
## Geneset
###################################################################
if "refseq_filename_gtf" in PARAMS:
    @split( (PARAMS["refseq_filename_gtf"], 
             PARAMS["refseq_filename_pep"], 
             PARAMS["refseq_filename_cdna"],
             PARAMS["refseq_filename_map"],
             PARAMS["refseq_filename_ensembl"],
             ),
            (PARAMS["ensembl_filename_gtf"],
             PARAMS["ensembl_filename_pep"],
             PARAMS["ensembl_filename_cdna"] ) )
    def importRefseq(infiles, outfiles ):
        '''convert a refseq gtf formatted file into an ensembl like
        gtf file.

        The refseq files should have been downloaded by USCS's
        table browser.

        Only unique refseq entries are used - all duplicates are
        removed. 

        This method imports the following files:
        gtf.gz, pep.fa.gz, cdna.fa.gz from the UCSC

        It also requires:
        
        * link.tsv.gz from the UCSC (table refLink)
           to add peptide identifiers and gene numbers.
        * ccdsinfo.tsv.gz from the UCSC (table ccdsInfo)
           to add a map from transcripts to ENSEMBL genes
        * refgene.tsv.gz from the UCSC (table refgene )
          to add gene_name. The refgene table contains
          most of the fields required for the gtf file,
          but unfortunately, the UCSC parser does
          not add it.
        '''

        infile_gtf, infile_pep, infile_cdna, infile_map, infile_ensembl = infiles
        outfile_gtf, outfile_pep, outfile_cdna = outfiles

        # build map between mrna and prot
        tmpfilename1 = P.getTempFilename()
        statement = '''gunzip < %(infile_map)s 
        | python %(scriptsdir)s/csv_cut.py mrnaAcc protAcc 
        | perl -p -e "s/\.\d+//g"
        > %(tmpfilename1)s
        '''
        P.run()

        # build map between mrna and gene - use ccds gene
        tmpfilename2 = P.getTempFilename()
        statement = '''gunzip < %(infile_map)s 
        | python %(scriptsdir)s/csv_cut.py mrnaAcc geneName
        | perl -p -e "s/\.\d+//g"
        > %(tmpfilename2)s
        '''
        P.run()

        statement = '''gunzip < %(infile_gtf)s
        | awk -v FS="\\t" -v OFS="\\t" '
              { $2 = "protein_coding"; print } '
        | python %(scriptsdir)s/gtf2gtf.py
           --remove-duplicates=ucsc
           --log=%(outfile_gtf)s.log
           --verbose=2
        | python %(scriptsdir)s/gtf2gtf.py
           --add-protein-id=%(tmpfilename1)s
           --log=%(outfile_gtf)s.log
           --verbose=2
        | python %(scriptsdir)s/gtf2gtf.py
           --rename=gene
           --apply=%(tmpfilename2)s
           --log=%(outfile_gtf)s.log
           --verbose=2
        | python %(scriptsdir)s/gtf2gtf.py
           --sort=gene
        | gzip
        > %(outfile_gtf)s'''
        if not os.path.exists(outfile_gtf):
            P.run()

        for infile, outfile in ( (infile_pep, outfile_pep),
                                 (infile_cdna, outfile_cdna)):
            # remove numerical suffixes from identifiers
            statement = '''gunzip < %(infile)s
            | perl -p -e "s/\.\d+//g" 
            | gzip 
            > %(outfile)s'''
            if not os.path.exists( outfile ):
                P.run()

        table = "ensembl2refseq"            
        # use ENSEMBL mapping
        if 0:
            outf = open(tmpfilename1, "w")

            reader = CSV.DictReader( IOTools.openFile(infile_ensembl), dialect="excel-tab" )
            c = E.Counter()

            outf.write("gene_id\ttranscript_id\trefseq_transcript_id\trefseq_protein_id\tccds_id\n")

            for row in reader:
                c.input += 1
                gene_id, transcript_id, refseq_transcript_id, refseq_protein_id, ccds_id = \
                    [ x.strip() for x in 
                      (row["Ensembl Gene ID"],
                       row["Ensembl Transcript ID"],
                       row["RefSeq DNA ID"],
                       row["RefSeq Protein ID"],
                       row["CCDS ID"],
                       ) ]

                if not (transcript_id and gene_id and refseq_transcript_id and refseq_protein_id):
                    c.skipped += 1
                    continue

                c.output += 1
                outf.write( "%s\t%s\t%s\t%s\t%s\n" %
                            (gene_id, transcript_id, refseq_transcript_id, refseq_protein_id, ccds_id))
            outf.close()

            statement = '''cat < %(tmpfilename1)s
            | csv2db.py %(csv2db_options)s 
               --index=gene_id 
               --index=transcript_id 
               --index=refseq_transcript_id 
               --index=refseq_protein_id 
               --index=ccds_id 
               --table=%(table)s
            > refseq.load'''

            P.run()
            E.info( "%s" % str(c))


        # use UCSC mapping
        statement = '''gunzip < %(infile_map)s
            | perl -p -i -e "s/\.\d+//g"
            | awk 'BEGIN {printf("ccds_id\\tsrc_db\\tttranscript_id\\tprotein_id\\n")} 
                   /^ccds/ {next} {print}'
            | csv2db.py %(csv2db_options)s 
               --index=ccds_id 
               --index=transcript_id 
               --index=protein_id 
               --table=%(table)s
            > refseq.load'''

        P.run()

        os.unlink( tmpfilename1 )
        os.unlink( tmpfilename2 )

if "refseq_filename_gtf" in PARAMS:
    @split( (PARAMS["refseq_filename_gtf"], 
             PARAMS["refseq_filename_pep"], 
             PARAMS["refseq_filename_cdna"],
             PARAMS["refseq_filename_map"],
             ),
            (PARAMS["ensembl_filename_gtf"],
             PARAMS["ensembl_filename_pep"],
             PARAMS["ensembl_filename_cdna"],
             "refseq.load" ) )
    def importRefseqFromUCSC(infiles, outfiles ):
        '''convert a refseq gtf formatted file into an ensembl like
        gtf file.

        The refseq files should have been downloaded by USCS's
        table browser.

        Only unique refseq entries are used - all duplicates are
        removed. 

        This method imports the following files:
        gtf.gz, pep.fa.gz, cdna.fa.gz from the UCSC

        It also requires:
        
        * link.tsv.gz from the UCSC (table refLink)
           to add peptide identifiers and gene numbers.
        * ccdsinfo.tsv.gz from the UCSC (table ccdsInfo)
           to add a map from transcripts to ENSEMBL genes
        * refgene.tsv.gz from the UCSC (table refgene )
          to add gene_name. The refgene table contains
          most of the fields required for the gtf file,
          but unfortunately, the UCSC parser does
          not add it.
        '''


        infile_gtf, infile_pep, infile_cdna, infile_map = infiles
        outfile_gtf, outfile_pep, outfile_cdna, outfile_load = outfiles

        if not os.path.exists( outfile_gtf ):
            PGeneset.importRefSeqFromUCSC( infile_gtf, outfile_gtf, remove_duplicates = True )

        for infile, outfile in ( (infile_pep, outfile_pep),
                                 (infile_cdna, outfile_cdna)):
            # remove numerical suffixes from identifiers
            statement = '''gunzip < %(infile)s
            | perl -p -e "s/\.\d+//g" 
            | gzip 
            > %(outfile)s'''
            if not os.path.exists( outfile ):
                P.run()

        # table = "ensembl2refseq"            

        # # use UCSC mapping
        # statement = '''gunzip < %(infile_map)s
        #     | perl -p -i -e "s/\.\d+//g"
        #     | awk 'BEGIN {printf("ccds_id\\tsrc_db\\tttranscript_id\\tprotein_id\\n")} 
        #            /^ccds/ {next} {print}'
        #     | csv2db.py %(csv2db_options)s 
        #        --index=ccds_id 
        #        --index=transcript_id 
        #        --index=protein_id 
        #        --table=%(table)s
        #     > %(outfile_load)s'''
        # P.run()

@files( ((None, "mgi.import"),))
def importMGI( infile, outfile ):
    '''create via BIOMART'''

    filename = "mgi_biomart.tsv" 

    if False:
        R.library("biomaRt")

        columns = {

            "marker_symbol_107" : "marker_symbol", 
            "marker_name_107" : "marker_name",
            "mgi_allele_id_att" : "allele_id",
            "allele_symbol_101" : "allele_symbol", 
            "allele_name_101" : "allele_name", 
            "allele_type_101" : "allele_type", 
            "phenotype_id_106_att" : "phenotype_id",
            "ensembl_gene_id_103" : "gene_id" }

        keys = columns.keys()

        mgi = R.useMart(biomart="biomart", dataset="markers")
        result = R.getBM( attributes=keys, mart=mgi )

        outf = open( filename, "w" )
        outf.write( "\t".join( [columns[x] for x in keys ] ) + "\n" )

        for data in zip( *[ result[x] for x in keys] ):
            outf.write( "\t".join( map(str, data) ) + "\n" )

        outf.close()

    if not os.path.exists( filename ):
        
        # associations need to be downloaded individually

        R.library("biomaRt")

        columns = {
            "mgi_marker_id_att" : "marker_id", 
            "marker_name_107" : "marker_name",
            "mgi_allele_id_att" : "allele_id",
            "allele_symbol_101" : "allele_symbol", 
            "allele_name_101" : "allele_name", 
            "allele_type_101" : "allele_type", 
            "phenotype_id_106_att" : "phenotype_id",
            "ensembl_gene_id_103" : "gene_id" }

        def downloadData( filename, columns ):
            '''download data via biomart into filename.
               translate column headers.'''

            if os.path.exists( filename): return

            E.info( "downloading data for %s" % filename )

            keys = columns.keys()

            mgi = R.useMart(biomart="biomart", dataset="markers")
            result = R.getBM( attributes=keys, 
                              mart=mgi )

            outf = open( filename, "w" )
            outf.write( "\t".join( [columns[x] for x in keys ] ) + "\n" )
            
            for data in zip( *[ result[x] for x in keys] ):
                outf.write( "\t".join( map(str, data) ) + "\n" )

            outf.close()

        downloadData( "mgi_marker2allele.tsv",
                      { "mgi_marker_id_att" : "marker_id", 
                        "mgi_allele_id_att" : "allele_id" } )

        downloadData( "mgi_allele2phenotype.tsv",
                      { "mgi_allele_id_att" : "allele_id",
                        "phenotype_id_106_att" : "phenotype_id" } )

        downloadData( "mgi_marker2gene.tsv",
                      { "mgi_marker_id_att" : "marker_id", 
                        "ensembl_gene_id_103" : "gene_id" } )

        downloadData( "mgi_markers.tsv",
                      { "mgi_marker_id_att" : "marker_id", 
                        "marker_symbol_107" : "symbol",
                        "marker_name_107" : "name",         
                        "marker_type_107": "type",         
                        } )

        downloadData( "mgi_alleles.tsv",
                      { "mgi_allele_id_att" : "allele_id",
                        "allele_symbol_101" : "symbol",
                        "allele_name_101" : "name",
                        "allele_type_101" : "type"
                        } )

        downloadData( "mgi_phenotypes.tsv",
                      { "phenotype_id_106_att" : "phenotype_id" ,
                        "term_106": "term" } )

        for filename in glob.glob("mgi_*.tsv"):
            tablename = filename[:-len(".tsv")]
            E.info( "loading %s" % tablename )

            # remove duplicate rows
            # remove rows where only the first field is set
            statement = '''
            perl -p -e "s/\\s+\\n/\\n/" < %(filename)s
            | %(scriptsdir)s/hsort 1
            | uniq
            | awk '{ for (x=2; x<=NF; x++) { if ($x != "") { print; break;} } }'
            | csv2db.py %(csv2db_options)s 
                     --table=%(tablename)s
                     --index=marker_id
                     --index=allele_id
                     --index=gene_id
                     --map=allele_name:str
                     --map=symbol:str
                     --index=phenotype_id
           >> %(outfile)s
           '''

            P.run()

        # for testing:        
        # columns = { "affy_hg_u95av2" : "affy",
        #             "hgnc_symbol" : "hgnc",
        #             "chromosome_name" : "chr",
        #             "band" : "band" }
        # mart = R.useMart("ensembl")
        # mart = R.useDataset("hsapiens_gene_ensembl",mart)
        # result = R.getBM(attributes=columns.keys(),
        #                  filters="affy_hg_u95av2",
        #                  values=("1939_at","1503_at","1454_at"), 
        #                  mart=mart)


        # all as one - incomplete data
        # columns = {
        #     "marker_symbol_107" : "marker_symbol", 
        #     "marker_name_107" : "marker_name",
        #     "mgi_allele_id_att" : "allele_id",
        #     "allele_symbol_101" : "allele_symbol", 
        #     "allele_name_101" : "allele_name", 
        #     "allele_type_101" : "allele_type", 
        #     "phenotype_id_106_att" : "phenotype_id",
        #     "ensembl_gene_id_103" : "gene_id" }

        # keys = columns.keys()

        # mgi = R.useMart(biomart="biomart", dataset="markers")
        # result = R.getBM( attributes=keys, mart=mgi )

        # outf = open( filename, "w" )
        # outf.write( "\t".join( [columns[x] for x in keys ] ) + "\n" )

        # for data in zip( *[ result[x] for x in keys] ):
        #     outf.write( "\t".join( map(str, data) ) + "\n" )

        # outf.close()
        
        
    # populate database - normalize at the same time
        
    # conversion = \
    #     (
    #     # marker/allele information
    #     { "table": "mgi_marker2allele",
    #       "columns" : ( "allele_id", "marker_id", ) 
    #       },
    #     { "table": "mgi_marker2ensembl",
    #       "columns" : ( "marker_id", "ensembl_id", ) 
    #       },
    #     { "table": "mgi_allele2phenotype",
    #       "columns" : ( "allele_id", "phenotype_id", ) 
    #       },
    #     { "table": "mgi_alleles",
    #       "columns" : ( "allele_id", "allele_name", "allele_symbol" ) 
    #       },
    #     { "table": "mgi_markers",
    #       "columns" : ( "marker_id", "marker_name", "marker_symbol" ) 
    #       },
    #     )

    # for convert in conversion:
        
    #     columns = " ".join( convert["columns"] )
    #     tablename = convert["table"]
    #     E.info( "loading %s" % tablename )

    #     statement = '''
    #         cat < %(filename)s
    #         | python %(scriptsdir)s/csv_cut.py %(columns)s
    #         | %(scriptsdir)s/hsort 1
    #         | uniq
    #         | csv2db.py %(csv2db_options)s 
    #                  --table=%(tablename)s
    #                  --index=marker_id
    #                  --index=allele_id
    #                  --index=gene_id
    #                  --map=allele_name:str
    #                  --index=phenotype_id
    #        > %(outfile)s
    #     '''

    #     P.run()
        
        
@files( ((None, "mgi.import"),))
def importMGIPhenotypesViaReports( infile, outfile ):
    '''
    MGI phenotype associations can be downloaded via biomart (1) from http://biomart.informatics.jax.org
    (2) reconstructed from their database dumps, or (3) from their SQL
    server.

    (1) Downloading via biomart has the advantage, that also
    alleles without phenotypes can be obtained. However, there
    is a manual step involved. Includes alleles without phenotypes.
    The table is not normalized.

    (2) Can be done automatically. However, the tables dumped are
    high-level views and not normalized. Also, not all alleles 
    are present, for example, MGI:4317524. This information is
    in MRK_GeneTrap.rpt, but crucially, the MGI id missing.
    The biomart table is more complete.

    Note that some tables only contain only the high-level 
    phenotype.

    Database schema:

    content        MGI format     column name
    mgi marker id  MGI:XXX        marker_id
    mgi allele id  MGI:XXX        allele_id
    phenotype id   MD:XXX         phenotype_id
    
    The files are mapped to the following database schema:

    A marker is associated with a coordinate:
       * file: MGI_Coordinate.rpt
       * file: mgi_marker_information

    A marker is associated with on or more alleles:
       * file: MGI_PhenotypicAllele.rpt, MGI_QTLAllele.rpt
       * table: mgi_marker2allele 
           
       note that the files KOMP_Allele.rpt and EUCOMM_Allele.rpt
       are part of MGI_PhenotypicAllele.rpt, but MGI_QTLAllele.rpt
       is distinct.

    A marker is associated with one or more ENSEMBL ids:
       * file: MRK_ENSEMBL.rpt
       * table: mgi_marker2ensembl

    An allele is associated with one or more mouse phenotypes
       * file: MGI_PhenoGenoMP.rpt
       * table: mgi_allele2phenotype

    Phenotype information
       * file: VOC_MammalianPhenotype.rpt
       * table: mgi_phenotype

    (3) Needs an account and requires specialized connectors to be 
        installed as their database is sybase.

    '''

    mgi_dir = "mgi"

    if not os.path.exists( mgi_dir ):
        os.mkdir (mgi_dir)

    conversion = \
        (
        # marker/allele information
        { "table": "mgi_marker2allele", "filename" : "MGI_PhenotypicAllele.rpt", 
          "columns" : ( "allele_id", "allele_symbol", "allele_name", "allele_type", "pmid", 
                        "marker_id", None, None, None, None ),
          "separator" : " " },

        # ensembl to marker
        { "table": "mgi_marker2ensembl", "filename" : "MRK_ENSEMBL.rpt", 
          "columns" : ("marker_id", None, None, None, None, "gene_id" ),
          "separator" : " " },

        # map of alleles to phenotypes
        { "table": "mgi_allele2phenotype", "filename" : "MGI_PhenoGenoMP.rpt", 
          "columns" : ( "allele_composition", "allele_symbol", "backgound", "phenotype_id", None, "marker_id" ) },

        # marker/allele information
        { "table": "mgi_marker2allele_qtl", "filename" : "MGI_QTLAllele.rpt", 
          "columns" : ( "allele_id", "allele_symbol", "allele_name", "allele_type", "pmid", 
                        "marker_id", None, None, None, None ) },

        # map of alleles to high-level phenotypes
        { "table": "mgi_allele2hl_phenotype", "filename" : "MGI_PhenotypicAllele.rpt", 
          "columns" : ( "allele_id", None, None, None, None,
                        None, None, None, None, "phenotype_id" ) },

        # phenotype information
        { "table": "mgi_phenotype", "filename" : "VOC_MammalianPhenotype.rpt", 
          "columns" : ( "phenotype_id", "brief", "description", ),
          "separator" : None },
        )

    # collect all filenames
    for filename in [x["filename"] for x in conversion]:
        target = os.path.join( mgi_dir, filename )
        if os.path.exists( target ): continue
        E.info( "downloading %s" % filename )
        statement = "wget -P %(mgi_dir)s ftp://ftp.informatics.jax.org/pub/reports/%(filename)s"
        P.run()
    
    for convert in conversion:
        tablename = convert["table"]

        tmpfile = P.getTempFile(".")
        headers, take = [], []
        for x,c in enumerate(convert["columns"]):
            if c != None:
                headers.append( c )
                take.append( x )

        tmpfile.write( "\t".join(headers) + "\n" )
        for line in open( os.path.join( mgi_dir, convert["filename"])):
            if line.startswith("#"): continue
            data = line[:-1].split("\t")
            tmpfile.write( "\t".join([data[x].strip() for x in take]) + "\n" )
            
        tmpfile.close()
        
        E.info( "creating table %s" % tablename )

        separator = convert.get( "separator", ",")

        tmpfilename = tmpfile.name 
        statement = '''
            cat < %(tmpfilename)s
            | python %(scriptsdir)s/table2table.py 
                     --separator="%(separator)s"
                     --split-fields
            | %(scriptsdir)s/hsort 1
            | uniq
            | csv2db.py %(csv2db_options)s 
                     --table=%(tablename)s
                     --index=marker_id
                     --index=allele_id
                     --index=gene_id
                     --map=allele_name:str
                     --index=phenotype_id
           > %(outfile)s
        '''

        P.run()

        os.unlink( tmpfilename )

@files( ((None, "ensembl2omim.tsv"),))
def importOMIMFromEnsembl( infile, outfile ):
    '''download gene id - OMIM associations via BIOMART.

    Note that missing numerical entries are set to -2147483648. 
    These will be set to 0.
    '''

    R.library("biomaRt")
    
    columns = {
        "ensembl_gene_id" : "gene_id",
        "mim_gene_accession" : "mim_gene_id",
        "mim_morbid_accession" : "mim_morbid_id",
        "mim_morbid_description" : "mim_morbid_description",
        }

    keys = columns.keys()

    mart = R.useMart(biomart="ensembl", dataset="hsapiens_gene_ensembl")
    result = R.getBM( attributes=keys, mart=mart )
    
    outf = open( outfile, "w" )
    outf.write( "\t".join( [columns[x] for x in keys ] ) + "\n" )
    
    for x in ("mim_gene_accession", "mim_morbid_accession"):
        result[x] = [ ("", y)[y >= 0] for y in result[x] ]

    for data in zip( *[ result[x] for x in keys] ):
        outf.write( "\t".join( map(str, data) ) + "\n" )

    outf.close()


###################################################################
###################################################################
###################################################################
## MAIN PIPELINE
###################################################################
###################################################################
###################################################################

###################################################################
###################################################################
###################################################################
## Targets for prepare
###################################################################

###################################################################
###################################################################
###################################################################
@files( ( (PARAMS["ensembl_ensembl2uniprot"], "ensembl2uniprot.load" ), ) )
def loadEnsembl2Uniprot( infile, outfile ):
    '''load mapping from ENSEMBL transcripts ids to
    uniprot ids.

    This method expects an BioMart output file with the following 
    five columns: 
    Ensembl gene id, 
    Ensembl transcript id, 
    Uniprot Swissprot Id,
    Uniprot Accession
    Uniport/Trembl Accession
    '''
    
    table = P.toTable( outfile )

    statement = '''gunzip 
    < %(infile)s
    | perl -p -e 
          "s/Ensembl Gene ID/gene_id/; 
           s/Ensembl Transcript ID/transcript_id/; 
           s/UniProt\/SwissProt ID/swissprot_id/;       
           s/UniProt\/SwissProt Accession/swissprot_acc/;
           s/UniProt\/TrEMBL Accession/trembl_acc/"
    | csv2db.py %(csv2db_options)s \
        --index=gene_id \
        --index=transcript_id \
        --index=trembl_acc \
        --table=%(table)s
    > %(outfile)s
    '''
    P.run()

###################################################################
###################################################################
###################################################################
## gene set section
############################################################
############################################################
############################################################
@files( PARAMS["ensembl_filename_gtf"], PARAMS['annotation'] )
def buildGeneRegions( infile, outfile ):
    '''annotate genomic regions with reference gene set.

    Only considers protein coding genes. In case of overlapping
    genes, only take the longest (in genomic coordinates).
    Genes not on UCSC contigs are removed.
    '''
    PGeneset.buildGeneRegions( infile, outfile )

############################################################
############################################################
############################################################
@follows( buildGeneRegions )
@files( PARAMS["ensembl_filename_gtf"], PARAMS['genes'] )
def buildGenes( infile, outfile ):
    '''build a collection of exons from the protein-coding
    section of the ENSEMBL gene set. The exons include both CDS
    and UTR.

    The set is filtered in the same way as in :meth:`buildGeneRegions`.
    '''
    PGeneset.buildProteinCodingGenes( infile, outfile )

############################################################
############################################################
############################################################
@files( PARAMS["ensembl_filename_gtf"], "gene_info.load" )
def loadGeneInformation( infile, outfile ):
    '''load the transcript set.'''
    PGeneset.loadGeneInformation( infile, outfile )

############################################################
############################################################
############################################################
@files( buildGenes, "gene_stats.load" )
def loadGeneStats( infile, outfile ):
    '''load the transcript set.'''

    PGeneset.loadGeneStats( infile, outfile )

############################################################
############################################################
############################################################
@files( PARAMS["ensembl_filename_gtf"], PARAMS["transcripts"] )
def buildTranscripts( infile, outfile ):
    '''build a collection of transcripts from the protein-coding
    section of the ENSEMBL gene set.

    Only CDS are used.
    '''
    PGeneset.buildProteinCodingTranscripts( infile, outfile )

############################################################
############################################################
############################################################
@transform( buildTranscripts, suffix(".gtf.gz"), "_gtf.load" )
def loadTranscripts( infile, outfile ):
    '''load the transcript set.'''
    PGeneset.loadTranscripts( infile, outfile )

############################################################
############################################################
############################################################
@files( buildTranscripts, "transcript_stats.load" )
def loadTranscriptStats( infile, outfile ):
    '''load the transcript set.'''

    PGeneset.loadTranscriptStats( infile, outfile )

############################################################
############################################################
############################################################
@files( PARAMS["ensembl_filename_gtf"], "transcript_info.load" )
def loadTranscriptInformation( infile, outfile ):
    '''load the transcript set.'''
    PGeneset.loadTranscriptInformation( infile, 
                                          outfile,
                                          only_proteincoding = PARAMS["ensembl_only_proteincoding"] )

###################################################################
###################################################################
###################################################################
@files( ( (PARAMS["ensembl_filename_pep"], PARAMS["peptides"] ), ) )
def buildPeptideFasta( infile, outfile ):
    '''load ENSEMBL peptide file
    
    *infile* is an ENSEMBL .pep.all.fa.gz file.
    '''
    PGeneset.buildPeptideFasta( infile, outfile )

###################################################################
###################################################################
###################################################################
@files( ( (PARAMS["ensembl_filename_cdna"], PARAMS["cdna"] ), ) )
def buildCDNAFasta( infile, outfile ):
    '''load ENSEMBL peptide file
    
    *infile* is an ENSEMBL .cdna.all.fa.gz file.
    '''
    PGeneset.buildCDNAFasta( infile, outfile )

###################################################################
###################################################################
###################################################################
@follows( loadTranscriptInformation )
@files( [(PARAMS["transcripts"], PARAMS["cds"] ),] )
def buildCDSFasta( infile, outfile ):
    '''build cds sequences from peptide and cds file.
    
    *infile* is an ENSEMBL .cdna.all.fa.gz file.
    '''

    PGeneset.buildCDSFasta( infile, outfile )

############################################################
############################################################
############################################################
@files( PARAMS["ensembl_filename_pep"], "protein_stats.load" )
def loadProteinStats( infile, outfile ):
    '''load the transcript set.'''

    PGeneset.loadProteinStats( infile, outfile )

############################################################
############################################################
############################################################
@merge( (loadProteinStats, loadTranscriptInformation), "seleno.list")
def buildSelenoList( infile, outfile ):
    '''export a list of seleno cysteine transcripts.'''

    dbhandle = sqlite3.connect( PARAMS["database"] )
    cc = dbhandle.cursor()
    statement = '''
    SELECT DISTINCT transcript_id
    FROM transcript_info as t,
         protein_stats as p
    WHERE p.protein_id = t.protein_id AND
         p.nU > 0
    '''
    outf = open(outfile, "w" )
    outf.write("transcript_id\n")
    outf.write("\n".join( [x[0] for x in cc.execute( statement) ] ) + "\n" )
    outf.close()

###################################################################
###################################################################
###################################################################
@files( PARAMS["ensembl_filename_gtf"], "annotations_bases.fasta" )
def buildBaseAnnotations( infile, outfile ):
    """build base annotations"""       

    to_cluster = True
    job_queue = "server_jobs.q"

    dbname = outfile[:-len(".fasta")]
    statement = """
        gunzip < %(infile)s |\
        python %(scriptsdir)s/gtf2fasta.py \
                --force \
                --genome=%(genome)s \
                --output-filename-pattern=annotations_bases.%%s \
                --log=%(outfile)s.log |\
        python %(toolsdir)s/index_fasta.py \
        --log=%(outfile)s.log \
        %(dbname)s - > %(outfile)s.log
    """

    P.run()

###################################################################
###################################################################
###################################################################
@files( PARAMS["ensembl_filename_gtf"], "annotations_exons.gtf" )
def buildExonAnnotations( infile, outfile ):
    """build exon annotations"""

    to_cluster = True
    job_queue = "server_jobs.q"
    
    statement = """
        gunzip < %(infile)s |\
        awk '$3 == "CDS"' |\
        python %(scriptsdir)s/gff2gff.py --sanitize=genome --skip-missing --genome-file=%(genome)s --log=%(outfile)s.log |\
        python %(scriptsdir)s/gtf2gff.py \
                --method=exons \
                --restrict-source=protein_coding \
                --log=%(outfile)s.log \
        > %(outfile)s
    """

    P.run()

###################################################################
###################################################################
###################################################################
@files( PARAMS["ensembl_filename_gtf"], "annotations_genes.gtf", )
def buildGeneAnnotations( infile, outfile ):
    """build gene annotations.

    Merge exons per gene within the reference set. The
    output includes the UTR and non-coding genes.
    """
    statement = """
        gunzip < %(infile)s |\
        python %(scriptsdir)s/gtf2gtf.py --merge-exons --with-utr --log=%(outfile)s.log |\
        python %(scriptsdir)s/gtf2gtf.py --set-transcript-to-gene --log=%(outfile)s.log |\
        python %(scriptsdir)s/gff2gff.py --skip-missing --sanitize=genome --genome-file=genome --log=%(outfile)s.log |\
        %(scriptsdir)s/gff_sort gene-pos \
        > %(outfile)s
    """
    queue = "server"
    P.run()

###################################################################
###################################################################
###################################################################
@files( buildGeneAnnotations, "annotations_genes.counts" )
def makeGeneCounts( infile, outfile ):
    """coun gene exon statistics.
    """
    
    statement = """
    cat < %(infile)s |\
    python %(scriptsdir)s/gtf2table.py \
        --genome-file=genome \
        --counter=length \
        --log=%(outfile)s.log \
    > %(outfile)s
    """ 
    P.run()

###################################################################
###################################################################
###################################################################
@follows( buildBaseAnnotations, buildExonAnnotations)
@transform(  "*.pileup.gz",
             suffix(".pileup.gz"), 
             ".annotations.gz" )
def makeAnnotations( infile, outfile ):
    """annotate snps with gene set."""

    to_cluster = True
    
    bases = "annotations_bases"

    statement = """
    gunzip < %(infile)s |\
    grep -v "^NT" |\
    python %(scriptsdir)s/snp2table.py \
        --genome-file=genome \
        --filename-annotations=%(bases)s \
        --log=%(outfile)s.log |\
    gzip > %(outfile)s
    """ 
    P.run()

###################################################################
###################################################################
###################################################################
@transform( makeAnnotations,
            suffix('.annotations.gz'), 
            '_annotations.load' )
def loadAnnotations( infile, outfile ):
    '''load annotations'''

    tablename = P.toTable( outfile )

    statement = ''' gunzip 
    < %(infile)s
    | csv2db.py %(csv2db_options)s 
              --map=gene_id:str 
              --index=gene_id 
              --table=%(tablename)s
              --map=base_qualities:text 
    > %(outfile)s
    '''
    P.run()

###################################################################
###################################################################
###################################################################
@transform( makeAnnotations,
            suffix('.annotations.gz'), 
            '_annotations.summary' )
def summarizeAnnotations( infile, outfile ):
    '''compute summary stats for annotation files.'''
    
    to_cluster = True

    # count substitutions for each category
    statement = '''gunzip 
    < %(infile)s
    | python %(scriptsdir)s/csv_cut.py code reference_base consensus_base variant_type 
    | awk '$4 == "variant_type" { printf("%%s-%%s-%%s\\tcounts\\n", $1,$2,$3); } 
           $4 == "E" || $4 == "O" {printf("%%s-%%s-%%s\\t1\\n", $1,$2,$3)}'
    | python %(scriptsdir)s/table2table.py --group=1 --group-function=sum 
    | perl -p -i -e "s/-/\\t/g unless (/^#/)"
    > %(outfile)s
    '''
    P.run()

###################################################################
###################################################################
###################################################################
@transform( summarizeAnnotations,
            suffix('_annotations.summary'), 
            '_annotations_summary.load' )
def loadAnnotationsSummary( infile, outfile ):
    '''load annotations'''

    tablename = P.toTable( outfile )

    statement = '''cat
    < %(infile)s
    | csv2db.py %(csv2db_options)s 
              --index=code
              --table=%(tablename)s
    > %(outfile)s
    '''
    P.run()

###################################################################
###################################################################
###################################################################
@follows( buildSelenoList )
@transform(  '*.pileup.gz', 
             suffix(".pileup.gz"), 
             ".effects.gz" )
def makeEffects( infile, outfile ):
    """annotate snps with gene set."""

    to_cluster = True
    
    seleno = "seleno.list"

    statement = """
    gunzip 
    < %(infile)s 
    | grep -v "^NT" 
    | python %(scriptsdir)s/snp2counts.py 
        --genome-file=genome 
        --module=transcript-effects 
        --filename-seleno=%(seleno)s 
        --filename-exons=%(transcripts)s 
        --output-filename-pattern=%(outfile)s.%%s.gz
        --log=%(outfile)s.log 
    | gzip 
    > %(outfile)s
    """ 
    P.run()

###################################################################
###################################################################
###################################################################
@transform(  makeEffects, 
             suffix(".effects.gz"), 
             "_effects.load" )
def loadEffects( infile, outfile ):
    '''load transcript effects into tables.'''

    root = infile[:-len(".effects.gz")]

    statement = '''
    csv2db.py %(csv2db_options)s \
              --from-zipped \
              --index=transcript_id \
              --table=%(root)s_effects \
    < %(infile)s > %(outfile)s
    '''
    P.run()

    for suffix in ("cds", "intron", "splicing", "translation"):
        
        statement = '''
        gunzip 
        < %(infile)s.%(suffix)s.gz
        | csv2db.py %(csv2db_options)s 
        --index=transcript_id 
        --table=%(root)s_effects_%(suffix)s 
        --ignore-column=seq_na
        --ignore-column=seq_aa
        >> %(outfile)s
        '''
        P.run()


###################################################################
###################################################################
###################################################################
@transform(  '*.pileup.gz', 
             suffix(".pileup.gz"), 
             add_inputs( buildTranscripts, buildSelenoList ),
             ".alleles" )
def buildAlleles( infiles, outfile ):
    """annotate snps with gene set."""

    to_cluster = True

    infile, transcripts, seleno = infiles

    statement = """gunzip < %(transcripts)s 
    | python %(scriptsdir)s/gtf2alleles.py 
        --genome-file=genome 
        --filename-seleno=%(seleno)s 
        --output-filename-pattern=%(outfile)s.%%s.gz
        --filename-pileup=%(infile)s
    > %(outfile)s
    """ 
    P.run()

###################################################################
###################################################################
###################################################################
@transform(  buildAlleles, 
             suffix(".alleles"), 
             "_alleles.load" )
def loadAlleles( infile, outfile ):
    '''load allele.'''

    tablename = outfile[:-len(".load")] 

    statement = '''gunzip
    < %(infile)s.table.gz
    | perl -p -e "s/False/0/g; s/True/1/g;"
    | csv2db.py %(csv2db_options)s 
              --index=gene_id 
              --index=transcript_id 
              --ignore-column=cds
              --ignore-column=peptide
              --table=%(tablename)s 
    > %(outfile)s
    '''
    P.run()

###################################################################
###################################################################
###################################################################
@transform(loadAlleles, 
           suffix("_alleles.load"),
           "_alleles_transcripts.load" )
def summarizeAllelesPerTranscript( infile, outfile ):
    '''summarize effects on a per-gene level.

    The following fields are exclusive:
    is_wildtype
       both alleles are wildtype
    is_knockout
       both alleles knocked out
    is_truncated
       both alleles truncated or truncated and knocked out
    is_affected
       one allele is truncated or knocked out

    The other fields are not necessarily exclusive, for example there
    could be transcripts with one knocked out allele and one wildtype
    allele, such that ``is_nmd_affected``, ``is_affected`` and ``has_wildtype`` 
    are all true.
    '''
    
    tablename = outfile[:-len(".load")]
    track = infile[:-len("_alleles.load")]

    dbhandle = sqlite3.connect( PARAMS["database"] )

    statement = '''
    CREATE TABLE %(tablename)s AS
    SELECT DISTINCT 
           transcript_id,
           COUNT(DISTINCT allele_id) AS nalleles,
           CASE WHEN SUM( is_nmd_knockout) = 2 THEN 1 ELSE 0 END AS is_nmd_knockout,
           CASE WHEN SUM( is_nmd_knockout) >= 1 THEN 1 ELSE 0 END AS is_nmd_affected,
           CASE WHEN SUM( is_splice_truncated) = 2 THEN 1 ELSE 0 END AS is_splice_truncated,
           CASE WHEN SUM( is_splice_truncated) >= 1 THEN 1 ELSE 0 END AS is_splice_affected,
           CASE WHEN SUM( is_stop_truncated) = 2 THEN 1 ELSE 0 END AS is_stop_truncated,
           CASE WHEN SUM( is_stop_truncated) >= 1 THEN 1 ELSE 0 END AS is_stop_affected,
           CASE WHEN SUM( is_wildtype ) = 2 THEN 1 ELSE 0 END AS is_wildtype, 
           CASE WHEN SUM( is_wildtype ) >= 1 THEN 1 ELSE 0 END AS has_wildtype, 
           contig AS contig, 
           strand AS strand, 
           GROUP_CONCAT( reference_first_stop_start ) AS stop_codons_start,
           GROUP_CONCAT( reference_first_stop_end ) AS stop_codons_end,
           0 AS is_knockout,
           0 AS is_truncated,
           0 AS is_affected
    FROM %(track)s_alleles AS a
    GROUP BY transcript_id
    ''' % locals()
    
    Database.executewait( dbhandle, "DROP TABLE IF EXISTS %(tablename)s" % locals() )
    Database.executewait( dbhandle, statement )
    Database.executewait( dbhandle, "CREATE INDEX %(tablename)s_transcript_id ON %(tablename)s (transcript_id)" % locals())
    Database.executewait( dbhandle, "UPDATE %(tablename)s SET is_knockout = is_nmd_knockout" % locals())
    Database.executewait( dbhandle, '''UPDATE %(tablename)s SET is_truncated = 
                                       is_splice_truncated OR is_stop_truncated OR 
                                       (is_splice_affected AND is_stop_affected) OR 
                                       (is_splice_affected AND is_nmd_affected) OR 
                                       (is_stop_affected AND is_nmd_affected)
                                       ''' % locals())
    Database.executewait( dbhandle, 'UPDATE %(tablename)s SET is_affected ='
                          '(is_nmd_affected OR is_splice_affected OR is_stop_affected) AND NOT'
                          '(is_knockout or is_truncated)'% locals())
    dbhandle.commit()

    P.touch(outfile)

###################################################################
###################################################################
###################################################################
@transform(summarizeAllelesPerTranscript, 
           suffix("_alleles_transcripts.load"),
           "_alleles_genes.load" )
def summarizeAllelesPerGene( infile, outfile ):
    '''summarize effects on a per-gene level.'''
    
    tablename = outfile[:-len(".load")]
    track = infile[:-len(".load")]

    dbhandle = sqlite3.connect( PARAMS["database"] )

    statement = '''
    CREATE TABLE %(tablename)s AS
    SELECT DISTINCT 
           i.gene_id AS gene_id,
           COUNT( DISTINCT a.transcript_id ) AS ntranscripts,
           CASE WHEN SUM( is_nmd_knockout ) = COUNT(DISTINCT a.transcript_id) THEN 1 ELSE 0 END AS is_nmd_knockout,
           SUM( is_nmd_knockout ) AS is_nmd_affected,
           CASE WHEN SUM( is_splice_truncated) = COUNT(DISTINCT a.transcript_id) THEN 1 ELSE 0 END AS is_splice_truncated,
           SUM( is_splice_truncated ) AS is_splice_affected,
           CASE WHEN SUM( is_stop_truncated ) = COUNT(DISTINCT a.transcript_id) THEN 1 ELSE 0 END AS is_stop_truncated,
           SUM( is_stop_truncated ) AS is_stop_affected,
           CASE WHEN SUM( is_wildtype ) = COUNT(DISTINCT a.transcript_id) THEN 1 ELSE 0 END AS is_wildtype, 
           SUM( is_wildtype ) AS has_wildtype, 
           contig AS contig, 
           strand AS strand, 
           GROUP_CONCAT( stop_codons_start ) AS stop_codons_start,
           GROUP_CONCAT( stop_codons_end ) AS stop_codons_end,
           0 AS is_knockout,
           0 AS is_truncated,
           0 AS is_affected
    FROM %(track)s AS a, transcript_info AS i
    WHERE i.transcript_id = a.transcript_id
    GROUP BY i.gene_id
    ''' % locals()
    
    Database.executewait( dbhandle, "DROP TABLE IF EXISTS %(tablename)s" % locals() )
    Database.executewait( dbhandle, statement )
    Database.executewait( dbhandle, "CREATE INDEX %(tablename)s_gene_id ON %(tablename)s (gene_id)" % locals())
    Database.executewait( dbhandle, "UPDATE %(tablename)s SET is_knockout = is_nmd_knockout" % locals())
    Database.executewait( dbhandle, '''UPDATE %(tablename)s SET is_truncated = 
                                       is_splice_truncated OR is_stop_truncated OR 
                                       (is_splice_affected + is_stop_affected >= ntranscripts)  
                          ''' % locals())
    Database.executewait( dbhandle, 'UPDATE %(tablename)s SET is_affected ='
                          '(is_nmd_affected OR is_splice_affected OR is_stop_affected) AND NOT'
                          '(is_knockout or is_truncated)'% locals())

    dbhandle.commit()

    P.touch(outfile)

###################################################################
###################################################################
###################################################################
@transform(loadEffects, 
           suffix("_effects.load"),
           "_effects_genes.load" )
def summarizeEffectsPerGene( infile, outfile ):
    '''summarize effects on a per-gene level.'''
    
    tablename = outfile[:-len(".load")]
    track = infile[:-len("_effects.load")]

    dbhandle = sqlite3.connect( PARAMS["database"] )

    statement = '''
    CREATE TABLE %(tablename)s AS
    SELECT DISTINCT 
           gene_id, 
           COUNT(*) AS ntranscripts,
           MIN(e.nalleles) AS min_nalleles,
           MAX(e.nalleles) AS max_nalleles,
           MIN(e.stop_min) AS min_stop_min,
           MAX(e.stop_min) AS max_stop_min,
           MIN(e.stop_max) AS min_stop_max,
           MAX(e.stop_max) AS max_stop_max,
           SUM( CASE WHEN stop_min > 0 AND cds_len - stop_min * 3 < last_exon_start THEN 1  
                     ELSE 0 END) AS nmd_knockout,
           SUM( CASE WHEN stop_max > 0 AND cds_len - stop_max * 3 < last_exon_start THEN 1  
                     ELSE 0 END) AS nmd_affected
    FROM transcript_info as i,
         %(track)s_effects AS e
    WHERE i.transcript_id = e.transcript_id
    GROUP BY i.gene_id
    ''' % locals()
    
    Database.executewait( dbhandle, "DROP TABLE IF EXISTS %(tablename)s" % locals() )
    Database.executewait( dbhandle, statement )
    Database.executewait( dbhandle, "CREATE INDEX %(tablename)s_gene_id ON %(tablename)s (gene_id)" % locals())
    dbhandle.commit()

    P.touch(outfile)
    

###################################################################
@follows( buildGeneAnnotations )
@files_re(  glob.glob( '*.pileup.gz'),
            '(.*).pileup.gz', 
            [r'\1.pileup.gz', "annotations_genes.gtf" ],
            r'\1.genecounts.gz' )
def makeSNPCountsPerGene( infiles, outfile ):
    """count snps within genes"""
    
    infile_snps, infile_genes = infiles

    statement = """
    gunzip < %(infile_snps)s |\
    grep -v "^NT" |\
    python %(scriptsdir)s/snp2counts.py \
        --genome-file=genome \
        --filename-exons=%(ensembl_filename_gtf)s \
        --log=%(outfile)s.log |\
    gzip > %(outfile)s
    """ 
    P.run()


###################################################################
@files( [ (None, "assignments.go" ), ] )
def createGO( infile, outfile ):
    '''get GO assignments from ENSEMBL'''
    PGO.createGO( infile, outfile )

############################################################
@files_re( createGO, "(.*).go", r"\1.goslim") 
def createGOSlim( infile, outfile ):
    '''get GO assignments from ENSEMBL'''
    PGO.createGOSlim( infile, outfile )


############################################################
@files( ( (importMGI, "assignments.mgi"),) )
def createMGI( infile, outfile ):
    '''get GO assignments from MGI'''

    dbhandle = sqlite3.connect( PARAMS["database"] )

    statement = '''
                 SELECT DISTINCT 'MPheno.ontology', m2g.gene_id, a2p.phenotype_id, p.term, 'NA' 
                 FROM mgi_marker2gene as m2g, 
                      mgi_marker2allele as m2a, 
                      mgi_allele2phenotype as a2p, 
                      mgi_phenotypes as p 
                 WHERE m2g.marker_id = m2a.marker_id AND 
                       a2p.allele_id = m2a.allele_id AND 
                       p.phenotype_id = a2p.phenotype_id
                ''' 
    cc = dbhandle.cursor()
    data = cc.execute(statement).fetchall()
    
    outf = open(outfile, "w")
    outf.write( "\n".join( [ "\t".join(x) for x in data ] ) + "\n" )
    outf.close()

###################################################################
@follows(summarizeEffectsPerGene, createGO, createGOSlim, createMGI)
@files( [ ("%s_effects_genes.load" % x, "%s_%s.%s" % (x,y,z), (x,y,z)) for x,y,z in 
      itertools.product( TRACKS_GO, 
                         ("nmdknockouttranscript",
                          "nmdaffectedtranscript",
                          "nmdknockoutgenes",
                          "nmdaffectedgenes"),
                         ("go", "goslim", "mgi") ) ] )
def runGOAnalysesOnEffects( infile, outfile, options ):
    '''run GO analysis on transcripts that have been knocked out
    by premature stop codons.

    ``options`` is a tuple of (``track``, ``analysis``, ``ontology``)

    ``analysis`` can be:

    nmdknockouttranscript
        genes for which one transcript has been knocked out
        due to NMD
    nmdaffectedtranscript
        genes in which one transcript is affected by NMD
    nmdknockoutgenes
        genes in which all transcripts have been knocked out 
        due to NMD

    '''
        
    track, analysis, ontology = options

    # setup foreground set
    if analysis == "nmdknockouttranscript":
        field_where = "e.nmd_knockout > 0"
    elif analysis == "nmdaffectedtranscript":
        field_where = "e.nmd_affected > 0"
    elif analysis == "nmdknockoutgenes":
        field_where = "e.nmd_knockout = e.ntranscripts"

    statement_fg = '''
    SELECT DISTINCT e.gene_id 
        FROM
            %(track)s_effects_genes AS e
        WHERE 
              %(field_where)s
        ORDER BY e.gene_id
    ''' % locals()

    # setup background set
    statement_bg = '''SELECT DISTINCT gene_id FROM gene_info''' % locals()
        
    # choose ontology
    if ontology == "go":
        gofile = "assignments.go"
    elif ontogoly == "goslim":
        gofile = "assignments.goslim"

    # create result directory
    outdir = os.path.abspath( outfile + ".dir" )
    try: os.makedirs( outdir )
    except OSError: pass

    # run
    PGO.runGOFromDatabase( outfile, 
                           outdir, 
                           statement_fg,
                           statement_bg,
                           gofile,
                           samples = 0)

###################################################################
@follows(summarizeAllelesPerGene, createGO, createGOSlim)
@files( [ ("%s_alleles_genes.load" % x, "%s_vs_%s.%s" % (x,y,z), (x,y,z)) for x,y,z in 
      itertools.product( TRACKS_GO, 
                         ("stoptruncated",
                          "nmdknockout",
                          "splicetruncated",
                          "knockout"),
                         ("goslim", 
                          "go",
                          "mgi",
                          ) ) ] )
def runGOAnalysesOnAlleles( infile, outfile, options ):
    '''run GO analysis on transcripts that have been knocked out
    by premature stop codons.

    ``options`` is a tuple of (``track``, ``analysis``, ``ontology``)

    ``analysis`` can be:

    stoptruncated
        genes that are truncated due to stops
    nmdknockout
        genes that are knocked out due to NMD
    splicetruncated
        genes that are truncated due to deleted splice sites
    knockout
        any of the above

    '''
        
    track, analysis, ontology = options

    # setup foreground set
    if analysis == "stoptruncated":
        field_where = "e.is_truncated"
    elif analysis == "nmdknockout":
        field_where = "e.is_nmd_knockout"
    elif analysis == "splicetruncated":
        field_where = "is_splice_truncated"
    elif analysis == "knockout":
        field_where = "(e.is_nmd_knockout or e.is_truncated or e.is_splice_truncated)"
    else:
        raise ValueError( "unknown analysis '%s'" % analysis )

    statement_fg = '''
    SELECT DISTINCT e.gene_id 
        FROM
            %(track)s_alleles_genes AS e
        WHERE 
              %(field_where)s
        ORDER BY e.gene_id
    ''' % locals()

    # setup background set
    statement_bg = '''SELECT DISTINCT gene_id FROM gene_info''' % locals()
        
    # choose ontology
    if ontology == "go":
        gofile = "assignments.go"
        ontology_file = PARAMS["go_ontology"]
    elif ontology == "goslim":
        gofile = "assignments.goslim"
        ontology_file = PARAMS["go_ontology"]
    elif ontology == "mgi":
        gofile = "assignments.mgi"
        ontology_file = PARAMS["mgi_ontology"]

    # create result directory
    outdir = os.path.abspath( outfile + ".dir" )
    try: os.makedirs( outdir )
    except OSError: pass

    # run
    PGO.runGOFromDatabase( outfile, 
                           outdir, 
                           statement_fg,
                           statement_bg,
                           gofile,
                           ontology_file = ontology_file,
                           samples = 1000 )
    
############################################################################
@merge( runGOAnalysesOnAlleles, "alleles_go.load")
def loadGOs( infile, outfile ):
    '''load go results.'''
    tablename = P.toTable( outfile )
    PGO.loadGOs( infile, outfile, tablename )

############################################################################
@transform( runGOAnalysesOnAlleles, suffix(".go"), "_go.load")
def loadGO( infile, outfile ):
    '''load go results.'''
    tablename = P.toTable( outfile )
    PGO.loadGO( infile, outfile, tablename )

############################################################################
@transform( runGOAnalysesOnAlleles, suffix(".goslim"), "_goslim.load")
def loadGOSlim( infile, outfile ):
    '''load goslim results.'''
    tablename = P.toTable( outfile )
    PGO.loadGO( infile, outfile, tablename )

############################################################################
@files( ((loadGOs, "goresults.table"),))
def mergeGO( infile, outfile ):
    '''merge all GO anlyses.

    * collect all P-Values for all categories and experiments.
    * compute stats on it
    '''

    dbhandle = sqlite3.connect( PARAMS["database"] )

    statement = '''SELECT track, geneset, annotationset, category, min(pover,punder) 
                   FROM alleles_go'''
    cc = dbhandle.cursor()
    data = cc.execute(statement).fetchall()
    
    pvalues = [ x[4] for x in data ]
    E.info( "analysing %i pvalues" % len(pvalues ))

    fdr = Stats.doFDR( pvalues )
    E.info( "got %i qvalues" % len(fdr.mQValues ))

    for d, qvalue in zip( data, fdr.mQValues ):
        if qvalue > 0.05: continue
        print data, qvalue
    
    Database.executewait( dbhandle, '''ALTER TABLE %(table)s ADD COLUMN is_coding FLOAT''' % locals())

############################################################################
@follows( mkdir( os.path.join( PARAMS["scratchdir"], "malis.dir" ) ) )
@merge( buildAlleles, "malis.map" )
def setupMultipleAlignment( infiles, outfile ):
    '''prepare input files for multiple alignment computations.

    This script does some id-mapping to resolve coordinates.

    Basically, each genome is separated into two alleles. 
    Gene_id's will be suffixed with the allele_id. This ensures
    that exons of a gene with multiple transcipts will be resolved
    correctly with consistent coordinates. 
 
    From an alignment point of view, the two alleles of the genes will be treated
    independently, but transcripts within a gene  will be merged correctly at exon 
    boundaries, again on a per-allele basis.

    Later, when collecting the results, the allele id is moved from the gene to
    the transcript.
    '''

    targetdir = os.path.join( PARAMS["scratchdir"], "malis.dir" )

    filepool_gtf = IOTools.FilePoolMemory( "%(targetdir)s/cluster_%%s.dir/cluster_%%s.gtf" % locals() )
    filepool_pep = IOTools.FilePoolMemory( "%(targetdir)s/cluster_%%s.dir/cluster_%%s_pep.fasta" % locals() )
    filepool_cds = IOTools.FilePoolMemory( "%(targetdir)s/cluster_%%s.dir/cluster_%%s_cds.fasta" % locals() )

    outf = open( outfile, "w")
    outf.write("id\tgroup_id\n")

    map_gene2group = {}
    map_seqid2code = {}
    x = 0
    counts = E.Counter()
    for infile in infiles:
        track = infile[:-len(".alleles")]
        E.info( "adding track %s" % track )

        reader = CSV.DictReader( open(infile+".table","rU"), dialect="excel-tab" )
        for row in reader:
            counts.input += 1
            gene_id, allele_id,transcript_id = row["gene_id"], row["allele_id"], row["transcript_id"]
            if gene_id not in map_gene2group:
                map_gene2group[gene_id] = len(map_gene2group)
            group_id = map_gene2group[gene_id]
            new_gene_id = "-".join( (gene_id, allele_id))
            if row["is_wildtype"] == "1": code = "WT"
            if row["is_nmd_knockout"] == "1": 
                counts.nmd_knockouts += 1
                continue

            else: code = "VA"
            seq_id = SEPARATOR.join( (track, transcript_id, new_gene_id ))
            map_seqid2code[seq_id] = code
            seq_id = SEPARATOR.join( (seq_id, code))
            outf.write( "%s\t%i\n" % (seq_id, group_id))
            filepool_pep.write( str(group_id), ">%s\n%s\n" % (seq_id, row["peptide"] ) )
            filepool_cds.write( str(group_id), ">%s\n%s\n" % (seq_id, row["cds"] ) )
            counts.written += 1

        with open(infile+".gtf") as inf:
            for gtf in GTF.iterator( inf ): 
                group_id = map_gene2group[gtf.gene_id]
                new_gene_id = "-".join( (gtf.gene_id, gtf["allele_id"]))
                seq_id = SEPARATOR.join( (track, gtf.transcript_id, new_gene_id ))
                seq_id = SEPARATOR.join( (seq_id, map_seqid2code[seq_id]))
                gtf.transcript_id = seq_id
                filepool_gtf.write( group_id, str(gtf) + "\n")

        x += 1
        # if x > 2: break

    E.info( "writing data" )
    filepool_gtf.close()
    filepool_pep.close()
    filepool_cds.close()
    outf.close()
    counts.ngroups = len(map_gene2group)
    counts.nsequences = len(map_seqid2code)

    E.info( "%s\n" % (str(counts)) )

@transform( os.path.join( PARAMS["scratchdir"], "malis.dir", "*", "*.gtf"), 
            suffix(".gtf"), 
            ".mali")
def buildMultipleAlignments( infile, outfile ):
    '''build multiple alignments.'''

    track = infile[:-len(".gtf")]
    filename_cds = track + "_cds.fasta"
    filename_pep = track + "_pep.fasta"

    to_cluster = True

    statement = '''
	python %(scriptsdir)s/align_transcripts.py \
		--gtf=%(infile)s \
		--cds=%(filename_cds)s \
		--force-map \
		--verbose=2 \
		--output-filename-pattern=%(track)s_%%s.fasta \
		--output=final_aa \
		--output=final_na \
		--output=aligned_aa \
		--output=aligned_na \
		--output-format="plain-fasta" \
	< %(filename_pep)s > %(outfile)s
      '''

    P.run()

@merge( buildMultipleAlignments, "variants" )
def buildMultipleAlignmentVariantColumns( infile, outfile ):
    '''build multiple alignments.'''

    track = infile[:-len(".gtf")]
    filename_cds = track + "_cds.fasta"
    filename_pep = track + "_pep.fasta"

    to_cluster = True

    statement = '''
	python %(scriptsdir)s/malis2mali.py \
		--gtf=%(infile)s \
		--cds=%(filename_cds)s \
		--force-map \
		--verbose=2 \
		--output-filename-pattern=%(track)s_%%s.fasta \
		--output=final_aa \
		--output=final_na \
		--output=aligned_aa \
		--output=aligned_na \
		--output-format="plain-fasta" \
	< %(filename_pep)s > %(outfile)s
      '''

    P.run()

@merge( buildMultipleAlignments, "malis.result" )
def mergeMultipleAlignments( infiles, outfile ):
    '''collect multiple alignment results into files that
    are compatible with OPTIC.
    '''

    for section in ("final_aa", "final_na", "aligned_aa", "aligned_na"):
        outfilename = outfile + "." + section + ".gz"

        counter = E.Counter()

        E.info("processing %s into %s" % (section, outfilename ))
        outf = gzip.open( outfilename, "w" )
        outf.write("cluster_id\tspecies\ttranscript_id\tgene_id\tcode\tsequence\n")
        for infile in infiles:
            counter.input += 1
            dirname, filename = os.path.split( infile )
            cluster_id = re.match("cluster_(\d+).mali", filename ).groups()[0]
            infilename = os.path.join( dirname, "cluster_%s_%s.fasta" % (cluster_id, section))
            # E.debug( "adding %s - %s from %s" % (filename, cluster_id, infilename) )
            if not os.path.exists(infilename):
                counter.missing += 1
                E.warn("multiple alignment %s missing" % infilename )
                continue
            for entry in FastaIterator.FastaIterator( open( infilename, "r")):
                parts = entry.title.split(SEPARATOR)
                if len(parts) == 4:
                    species, transcript_id, gene_id, code = entry.title.split(SEPARATOR)
                elif len(parts) == 2:
                    species, gene_id = entry.title.split(SEPARATOR)
                    transcipt_id = gene_id
                    code = "CG"
                # transfer the allele_id from the gene to the transcript
                gene_id, allele_id = gene_id.split("-")
                transcript_id += "-" + allele_id

                outf.write( "\t".join(map(str,
                                          (cluster_id,
                                           species,
                                           transcript_id,
                                           gene_id,
                                           code,
                                           entry.sequence))) + "\n")
            counter.output += 1

        outf.close()
        E.info( "%s: %s" % (outfilename, str(counter)))

    P.touch(outfile)

@merge( '*_pileup.load', 
        "genome.maf.gz" )
def buildMAF( infiles, outfile ):

    tracks = " ".join( ["--track=%s" % x[:-len(".load")] for x in infiles] )

    statement = '''
    gunzip 
    < transcripts.gtf.gz 
    | python %(scriptsdir)s/gtf2gtf.py
           --merge-transcripts --with-utr 
    | %(cmd-farm)s --split-at-lines=100 --log=%(outfile)s.log --binary -v 10 
    "python %(scriptsdir)s/snp2maf.py 
          --genome=genome 
          %(tracks)s 
          --reference=mm9 
          --is-gtf 
          --pattern='\(\\\\\\S+\)_pileup'
          --log=%(outfile)s.log" | gzip
    > %(outfile)s
    ''' 

    P.run()

###################################################################
###################################################################
###################################################################
@merge(summarizeAllelesPerGene, 
       "export/nmd_knockouts.tsv.gz" )
def exportKnockoutLists( infiles, outfile ):
    
    outf = gzip.open( outfile, "w")
    
    headers = ("strain",
               "gene_id", 
               "gene_name", 
               "ntranscripts", 
               "contig",
               "strand",
               "stops-start",
               "stops-end" )
    
    outf.write("%s\n" % "\t".join(headers))

    dbhandle = sqlite3.connect( PARAMS["database"] )

    for infile in infiles:

        track = infile[:-len(".load")]
        strain = track[:-len("_alleles_genes")]

        statement = '''
                     SELECT DISTINCT '%(strain)s',
                            g.gene_id, 
                            i.gene_name, 
                                    g.ntranscripts,
                                    g.contig, g.strand,
                                    g.stop_codons_start,
                                    g.stop_codons_end
                         FROM %(track)s as g,
                            transcript_info AS i
                        WHERE g.gene_id = i.gene_id AND g.is_nmd_knockout
        ''' % locals()
        
        print statement
        outf.write( "\n".join( ["\t".join(map(str,x)) \
                                    for x in Database.executewait( dbhandle, statement ).fetchall() ] ) + "\n" )

    outf.close()

###################################################################
###################################################################
###################################################################
@merge( "*_effects.load", "polyphen.input" )
def buildPolyphenInput( infiles, outfile ):
    '''build polyphen input file.

    SNPS across all species are aggregated into a single
    file to avoid multiple submissions for the same variant.

    Mapping to Uniprot ids was not successful - 40% of the
    SNPs would have been lost. Hence I map to ensembl protein
    identifiers. Note that the sequence file is then to be 
    submitted to POLYPHEN as well.

    Note that this method outputs 1-based coordinates for polyphen,
    while the coordinates in the .map file are still 0-based.

    SNPs are assigned a snp_id and a locus_id. The snp_id refers
    to the SNP within a peptide sequence while the locus_id refers
    to the genomic location. If there are alternative
    transcripts overlapping a SNP, the same SNP will get two
    snp_ids, but the same locus_id. As the peptide background might
    be different for the same SNP depending on the transcript,
    its effect needs to be predicted twice.
    '''
    
    statement = '''SELECT
        transcript_id,
        cds_start,
        cds_end,
        orig_codons,
        variant_codons,
        orig_na,
        variant_na,
        contig,
        snp_position
    FROM %(table)s_cds
    WHERE variant_code = '=' AND code = 'N'
    '''

    dbhandle = sqlite3.connect( PARAMS["database"] )
    cc = dbhandle.cursor()

    infiles.sort()

    # uniprot mapping:
    #map_transcript2id = dict( 
    #cc.execute( "SELECT transcript_id, trembl_acc FROM ensembl2uniprot WHERE trembl_acc IS NOT NULL").fetchall() )

    # ensembl mapping
    map_transcript2id = dict(
        cc.execute( "SELECT transcript_id, protein_id FROM transcript_info WHERE protein_id IS NOT NULL").fetchall() )

    total_counts = E.Counter()
    notfound, found = set(), set()

    outf_map = open( outfile + ".map", "w" )
    outf_map.write( "snp_id\ttrack\ttranscript_id\tprotein_id\tprotein_pos\tlocus_id\tcontig\tpos\tphase\n" )
    
    outf = open( outfile, "w" )
    
    snps = {}
    locus_ids = {}

    for infile in infiles:

        table = P.toTable( infile ) 
        track = table[:-len("_effects")]
        cc.execute(statement % locals())

        counts = E.Counter()

        snp_id = 0
        for transcript_id, cds_start, cds_end, orig_codons, variant_codons, orig_na, variant_na, contig, pos in cc:

            counts.input += 1

            if transcript_id not in map_transcript2id:
                notfound.add( transcript_id )
                counts.not_found += 1
                continue

            if "," in variant_codons:
                counts.heterozygous += 1
                continue
            
            for phase in range(0,3):
                if orig_na[phase].lower() != variant_na[phase].lower(): 
                    break

            pid = map_transcript2id[transcript_id]
            # one-based coordinates
            peptide_pos = int(math.floor(cds_start / 3.0)) + 1
            key = "%s-%i-%s" % (pid, peptide_pos,variant_codons)

            if key in snps: 
                snp_id = snps[key]
            else:
                snp_id = len(snps)
                snps[key] = snp_id
                outf.write( "snp%010i\t%s\t%i\t%s\t%s\n" % \
                                (snp_id,
                                 pid,
                                 peptide_pos,
                                 orig_codons,
                                 variant_codons,
                                 ) )
                counts.output += 1                

            locus_key = "%s-%i-%s" % (contig,pos,variant_codons)
            if locus_key not in locus_ids:
                locus_ids[locus_key] = len(locus_ids)
            
            # use 0-based coordinates throughout, including peptide pos
            outf_map.write( "snp%010i\t%s\t%s\t%s\t%i\tloc%010i\t%s\t%i\t%i\n" % \
                                (snp_id, 
                                 track,
                                 transcript_id,
                                 pid,
                                 peptide_pos-1,
                                 locus_ids[locus_key],
                                 contig, 
                                 pos,
                                 phase) )

            found.add( transcript_id )

        total_counts += counts

        E.info( "%s: %s" % (table, str(counts) ))

    outf.close()
    outf_map.close()

    E.info( "%s: transcripts: %s found, %i not found" % (table,
                                                         len(found),
                                                         len(notfound)))

    E.info( "total=%s, snp_ids=%i, locus_ids=%i" % (str(total_counts), len(snps), len(locus_ids) ))
    if notfound:
        E.warn( "%i transcripts had SNPS that were ignored because there was no uniprot accession" % len(notfound))
        E.warn( "notfound: %s" % ",".join( notfound))

    statement = '''sort -k2,2 -k3,3n %(outfile)s > %(outfile)s.tmp; mv %(outfile)s.tmp %(outfile)s'''

    P.run( )

###################################################################
###################################################################
###################################################################
@transform( (buildPolyphenInput, "*.input"), suffix(".input"), ".features")
def buildPolyphenFeatures( infile, outfile ):
    '''run polyphen on the cluster.

    To do this, first send uniref to all nodes:

    python ~/cgat/cluster_distribute.py 
           --collection=andreas 
           /net/cpp-group/tools/polyphen-2.0.18/nrdb/uniref100*.{pin,psd,psi,phr,psq,pal}
    '''
    
    nsnps = len([ x for x in open(infile)])

    to_cluster = True
    stepsize = max( int(nsnps / 200000.0), 1000 )
    job_array=(0, nsnps, stepsize)
    E.info("running array jobs on %i snps" % nsnps )

    scratchdir = os.path.join(os.path.abspath("."), "scratch")
    try:
        os.mkdir( scratchdir )
    except OSError:
        pass

    resultsdir = outfile + ".dir"
    try:
        os.mkdir( resultsdir )
    except OSError:
        pass

    statement = '''
    /net/cpp-group/tools/polyphen-2.0.18/bin/run_pph_cpp.pl
       -s %(peptides)s
       -b %(polyphen_blastdb)s
       -d %(scratchdir)s
       %(infile)s > %(resultsdir)s/%(outfile)s.$SGE_TASK_ID 2> %(resultsdir)s/%(outfile)s.err.$SGE_TASK_ID
    '''
    P.run()

    to_cluster = False
    job_array=None

    statement = '''find %(resultsdir)s -name "*.err.*" -exec cat {} \; > %(outfile)s.log'''
    P.run()

    statement = '''find %(resultsdir)s -not -name "*.err.*" -exec cat {} \; > %(outfile)s'''
    P.run()

###################################################################
###################################################################
###################################################################

@files( [ ( buildPolyphenFeatures, "polyphen_%s.output.gz" % x, x ) for x in P.asList( PARAMS["polyphen_models"] ) ] )
def runPolyphen( infile, outfile, model ):
    '''run POLYPHEN on feature tables to classify SNPs.
    '''

    to_cluster = True

    # options
    # -p: print header
    # -h: skip header (not used)
    # -f: feature set, default is F11
    # -c: classifier, default is NBd (Naive Bayes with discretization)
    # -l: model name, default is HumDiv

    statement = '''
    %(polyphen_home)s/bin/run_weka.pl 
           -l %(polyphen_home)s/models/%(model)s.UniRef100.NBd.f11.model
           -p %(infile)s 
    | gzip 
    > %(outfile)s 
    2> %(outfile)s.log
    '''
    
    P.run()

###################################################################
###################################################################
###################################################################
@transform( buildPolyphenInput, suffix(".input"), "_map.load" )
def loadPolyphenMap( infile, outfile ):
    '''load polyphen input data.'''

    table = P.toTable( outfile )
    statement = '''
    csv2db.py %(csv2db_options)s
              --index=snp_id 
              --index=track,transcript_id
              --index=contig,pos
              --index=protein_id
              --index=transcript_id
              --table=%(table)s 
    < %(infile)s.map
    > %(outfile)s
    '''
    P.run()

###################################################################
###################################################################
###################################################################
@transform( runPolyphen, suffix(".output.gz"), ".load")
def loadPolyphen( infile, outfile ):
    '''load polyphen results.'''
    
    table = P.toTable( outfile )

    statement = '''
    gunzip 
    < %(infile)s
    | perl -p -e "s/o_acc/protein_id/; s/ +//g"
    | csv2db.py %(csv2db_options)s
              --index=snp_id 
              --index=protein_id
              --table=%(table)s 
              --map=effect:str
    > %(outfile)s
    '''
    P.run()

###################################################################
###################################################################
###################################################################
@transform( loadPolyphen, suffix(".load"), ".genestats")
def analysePolyphen( infile, outfile ):
    '''compute enrichment of SNPs within genes
    and deleterious SNPs within SNPs within genes.

    del: enrichment of deleterious snps within snps per gene
    len: enrichment of snps within genes
    com: enrichment of deleterious snps within gene
    '''
    
    table = P.toTable( infile )
    tablename_map = "polyphen_map"

    dbhandle = sqlite3.connect( PARAMS["database"] )
    cc = dbhandle.cursor()

    statement = '''
        SELECT i.gene_id,
               COUNT(DISTINCT map.locus_id) as nsnps, 
               COUNT(DISTINCT case t.prediction when 'possiblydamaging' then map.locus_id when 'probablydamaging' then map.locus_id else NULL end) AS ndeleterious,
               MAX(s.length)
               FROM %(table)s as t, 
                    %(tablename_map)s as map, 
                    protein_stats as s,
                    transcript_info as i 
        WHERE map.snp_id = t.snp_id AND 
              i.transcript_id = map.transcript_id AND
              s.protein_id = map.protein_id
        GROUP BY i.gene_id
     ''' % locals()

    data = cc.execute(statement).fetchall()

    statement = '''SELECT DISTINCT i.gene_id, MAX(s.length) 
                   FROM transcript_info AS i, protein_stats AS s 
                   WHERE s.protein_id = i.protein_id 
                   GROUP BY i.gene_id'''
    gene_ids = cc.execute(statement).fetchall()

    total_nsnps = sum( [ x[1] for x in data ] )
    total_ndel = sum( [ x[2] for x in data ] )
    total_length = sum( [ x[1] for x in gene_ids ] )
    del_p = float(total_ndel) / total_nsnps
    len_p = float(total_nsnps) / total_length
    com_p = float(total_ndel) / total_length

    E.info( "del: background probability: %i/%i = %f" % (total_ndel, total_nsnps, del_p ) )
    E.info( "len: background probability: %i/%i = %f" % (total_nsnps, total_length, len_p ) )
    E.info( "com: background probability: %i/%i = %f" % (total_ndel, total_length, com_p ) )

    outf = open( outfile, "w" )
    outf.write( "\t".join( ("gene_id", "code", 
                            "length", "nsnps", "ndel", 
                            "del_p", "del_pvalue", "del_qvalue", 
                            "len_p", "len_pvalue", "len_qvalue",
                            "com_p", "com_pvalue", "com_qvalue", ) ) + "\n" )

    del_pvalues, len_pvalues, com_pvalues = [], [], []
    for gene_id, nsnps, ndel, length in data:

        # use -1, because I need P( x >= X)
        # sf = 1 - cdf and cdf = P( x <= X ), thus sf = 1 - P( x <= X ) = P (x > X ).
        del_pvalues.append( scipy.stats.binom.sf( ndel - 1, nsnps, del_p ) ) 
        len_pvalues.append( scipy.stats.binom.sf( nsnps - 1, int(round(length)), len_p ) )
        com_pvalues.append( scipy.stats.binom.sf( ndel - 1, int(round(length)), com_p ) )

    del_q = Stats.doFDR( del_pvalues )
    len_q = Stats.doFDR( len_pvalues )
    com_q = Stats.doFDR( com_pvalues )

    fdr = PARAMS["polyphen_fdr"]

    found = set()

    for a, del_pvalue, del_qvalue, len_pvalue, len_qvalue, com_pvalue, com_qvalue in \
            zip(data, 
                del_pvalues, del_q.mQValues, 
                len_pvalues, len_q.mQValues,
                com_pvalues, com_q.mQValues,
                ):
        gene_id, nsnps, ndel, length = a
        found.add(gene_id)

        del_p = float(ndel) / nsnps
        len_p = float(nsnps) / length

        code = "".join( [ str(int(x < fdr)) for x in (del_qvalue, len_qvalue, com_qvalue) ] )

        outf.write( "\t".join( (gene_id,
                                code,
                                "%i" % int(round(length)),
                                "%i" % int(nsnps),
                                "%i" % int(ndel),
                                "%6.4f" % del_p,
                                "%6.4g" % del_pvalue,
                                "%6.4g" % del_qvalue,
                                "%6.4f" % len_p,
                                "%6.4g" % len_pvalue,
                                "%6.4g" % len_qvalue,
                                "%6.4f" % com_p,
                                "%6.4g" % com_pvalue,
                                "%6.4g" % com_qvalue,
                                ) ) + "\n" )

    # add missing genes:
    code = "---"
    for gene_id, length in gene_ids:
        if gene_id in found: continue
        outf.write( "\t".join( (gene_id,
                                code,
                                "%i" % int(round(length)),
                                "%i" % 0,
                                "%i" % 0,
                                "%6.4f" % 0,
                                "%6.4g" % 1,
                                "%6.4g" % 1,
                                "%6.4f" % 0,
                                "%6.4g" % 1,
                                "%6.4g" % 1,
                                "%6.4f" % 0,
                                "%6.4g" % 1,
                                "%6.4g" % 1,
                                ) ) + "\n" )

    outf.close()

    

###################################################################
###################################################################
###################################################################
@transform( analysePolyphen, suffix(".genestats"), "_genestats.load")
def loadPolyphenAnalysis( infile, outfile ):
    '''load polyphen analysis results.'''
    
    table = P.toTable( outfile )

    statement = '''
    cat < %(infile)s
    | csv2db.py %(csv2db_options)s
              --index=gene_id 
              --map=code:str
              --table=%(table)s 
    > %(outfile)s
    '''
    P.run()

###################################################################
###################################################################
###################################################################
@files( ( ( buildPeptideFasta, "panther.scores"), ))
def preparePanther( infile, outfile ):
    '''lookup peptide sequences with panther.

    The actual snps will get scored in the next step.
    This step takes a while, but could be sped up
    easily by parallelization.
    '''

    to_cluster = True

    if type(infile) in (types.ListType, types.TupleType):
        infile = infile[0]

    tmpdir = P.getTempDir( "." )

    statement = '''
    (PERL5LIB=%(panther_home)s/lib:$PERL5LIB;
     perl %(panther_home)s/pantherScore.pl
                      -l %(panther_library)s
                      -D B -V -n
                      -i %(infile)s
                      -o %(outfile)s
                      -T %(tmpdir)s )
    '''

    P.run()

    shutil.rmtree( tmpdir )

###################################################################
###################################################################
###################################################################
@files( ( ( (buildPolyphenInput, preparePanther), "panther.output" ), ) )
def runPanther( infiles, outfile):
    '''run PANTHER analysis.
    '''

    # to_cluster = True

    filename_snps, filename_scores = infiles

    tmpdir = P.getTempDir( "." )

    peptides = PARAMS["peptides"]
    tmpfilename_snps = P.getTempFilename(".")

    statement = '''
    awk '{printf("%%s|%%s|%%s|%%s;%%s\\n",
                 $1,$2,$3,$4,$5);}'
    < %(filename_snps)s > %(tmpfilename_snps)s
    '''
    # P.run()

    statement = '''
    (PERL5LIB=%(panther_home)s/lib:$PERL5LIB;
     PATH=%(panther_home)s:$PATH;
     awk '{printf("%%s|%%s|%%s|%%s;%%s\\n",
                 $1,$2,$3,$4,$5);}'
    < %(filename_snps)s
    | %(cmd-farm)s --split-at-lines=2000 --log=%(outfile)s.log -v 10 --output-header --env=PERL5LIB --env=PATH
    "perl %(panther_home)s/snp_analysis.pl
                      -l %(panther_library)s
                      -c %(filename_scores)s
                      -s %%STDIN%%
                      -f %(peptides)s
                      -b %(panther_home)s/BLOSUM62
                      -V
                      -p %(panther_home)s/uprior.9comp
                      -o %%STDOUT%%
                      -T %(tmpdir)s"
    > %(outfile)s 2> %(outfile)s.log )
    '''

    P.run()
    shutil.rmtree( tmpdir )
    os.unlink( tmpfilename_snps )


###################################################################
###################################################################
###################################################################
@transform( runPanther, suffix(".output"), ".load")
def loadPanther( infile, outfile ):
    '''load panther results.'''

    table = P.toTable( outfile )

    statement = '''
    perl -p -e "s/snpId/snp_id/; s/seqId/protein_id/; s/HMM /hmm/g;"
    < %(infile)s
    | csv2db.py %(csv2db_options)s
              --index=snp_id
              --index=protein_id
              --table=%(table)s
    > %(outfile)s
    '''
    P.run()

###################################################################
###################################################################
###################################################################
@split( loadPolyphenMap, ("counts_shared.matrix"
                          , "counts_segregation.matrix"
                          , "counts_pid.matrix"
                          , "counts_distance.matrix"
                          , "counts.tree"
                          ) )
def buildSharedSNPMatrix( infiles, outfiles ):
    '''build matrix of shared coding nonsynonymous SNPs.

    Counts are per locus id.

    Percent identities are only within coding segregating loci
    and thus do not reflect the real divergence.

    '''
    
    dbhandle = sqlite3.connect( PARAMS["database"] )
    cc = dbhandle.cursor()

    segregating_sites = cc.execute('SELECT COUNT( DISTINCT locus_id) FROM polyphen_map').fetchone()[0]
    
    statement = '''SELECT DISTINCT locus_id, track FROM polyphen_map ORDER BY locus_id'''
    cc.execute(statement)

    matrix = collections.defaultdict( int )
    for k, vals in itertools.groupby( cc, key = lambda x: x[0] ):
        tracks = [x[1] for x in list(vals)] 
        for t1 in tracks:
            matrix[(t1,t1)] += 1
        if len(tracks) > 1:
            for t1, t2 in itertools.combinations( tracks, 2):
                matrix[(t1,t2)] += 1
                matrix[(t2,t1)] += 1

    all_tracks = set( [ x[0] for x in matrix.keys()] + [x[1] for x in matrix.keys() ] )

    
    # output matrix with shared SNPs.
    outf = open(outfiles[0],"w")
    outf.write( "track\t%s\n" % "\t".join(all_tracks))
    for track1 in all_tracks:
        outf.write( "%s" % track1)
        for track2 in all_tracks:
            outf.write("\t%i" % matrix[(track1,track2)])
        outf.write("\n")
    outf.close()


    # output matrix with shared segregating sites as 
    # distance matrix
    outf = open(outfiles[1],"w")
    outf.write( "track\t%s\n" % "\t".join(all_tracks))
    for track1 in all_tracks:
        outf.write( "%s" % track1)
        for track2 in all_tracks:
            if track1 == track2:
                outf.write("\t%i" % 0)
            else:
                outf.write("\t%i" % (segregating_sites - matrix[(track1,track2)]))
        outf.write("\n")
    outf.close()

    # output matrix as percent identity matrix
    # percent identity is given as 
    # segregating sites - sites where strains differ = segregating_sites - (matrix[i,i] + matrix[j,j] - 2 * matrix[i,j])
    # simplifies to:
    # segsites - matrix[i,i] -matrix[j,j] +
    # divided by the total number of segregating sites
    outf = open(outfiles[2],"w")
    outf.write( "track\t%s\n" % "\t".join(all_tracks))
    pids = {}
    for track1 in all_tracks:
        outf.write( "%s" % track1)
        for track2 in all_tracks:
            a = segregating_sites - (matrix[(track1,track1)] + matrix[(track2,track2)] - 2 * matrix[(track1,track2)])
            pid = 100.0 *  a/ segregating_sites
            outf.write("\t%6.4f" % pid)
            pids[(track1,track2)] = pid
        outf.write("\n")
    outf.close()

    # distance matrix
    outf = open(outfiles[3],"w")
    outf.write( "track\t%s\n" % "\t".join(all_tracks))
    for track1 in all_tracks:
        outf.write( "%s" % track1)
        for track2 in all_tracks:
            val = 100.0 - pids[(track1,track2)]
            outf.write("\t%6.4f" % val)
        outf.write("\n")
    outf.close()

    outfile_distance, outfile_tree = outfiles[3], outfiles[4]

    # build tree
    statement = '''python %(scriptsdir)s/matrix2matrix.py
       --output-format=phylip
    < %(outfile_distance)s
    | python %(scriptsdir)s/matrix2tree.py
       --method=nj
    > %(outfile_tree)s
    '''
    P.run()

###################################################################
###################################################################
###################################################################
## Enrichment analysis
###################################################################
@files( ((None, "workspace_genomic.bed", "genomic" ),
         ) )
def buildEnrichmentWorkspaces( infile, outfile, workspace ):
    PEnrichment.buildWorkSpace( outfile, workspace )

@files( (("%s.fasta" % PARAMS["genome"], "workspace_isochores.bed.gz" ), ) )
def buildEnrichmentIsochores( infile, outfile ):
    PEnrichment.buildIsochoresGC( infile, outfile )

@follows( mkdir( "enrichment.dir") )
@transform( "*_effects.load", regex("(.*)_effects.load"), r"enrichment.dir/\1.bed.gz" )
def buildDeleteriousSNPs( infile, outfile ):

    track = infile[:-len("_effects.load")]
    
    outf = gzip.open(outfile, "w")
    outf.write( "track name=%s\n" % track )

    dbhandle = sqlite3.connect( PARAMS["database"] )
    cc = dbhandle.cursor()

    statement = '''SELECT DISTINCT map.contig, map.pos 
                          FROM polyphen_map AS map,
                          polyphen_HumDiv as result
                          WHERE map.track = '%(track)s'
                                AND map.snp_id = result.snp_id
                                AND (result.prediction = 'possiblydamaging'
                                    OR result.prediction = 'probablydamaging')
                          ''' % locals()

    cc.execute(statement)
    
    for contig, pos in cc:
        outf.write( "%s\t%i\t%i\n" % (contig, pos, pos+1) )
        
    outf.close()

@files( ((None, "enrichment.table" ), ) )
def runGAT( infiles, outfile ):
    '''run Enrichment analysis
    '''

    workspaces = [ "genome.bed", ]
    annotations = [ "/net/cpp-compute/backup/andreas/projects/mousestrains/data/qtl/martins/merged.bed", ]

    workspaces = " ".join( [ "--workspace=%s" for x in workspaces ] )
    annotations = " ".join( [ "--annotation=%s" for x in annotations ] )
    segments = " ".join( [ "--segments=%s" for x in segments ] )

    statement = '''gatrun.py
                  %(workspaces)s
                  %(segments)s
                  %(annotations)s
                  --num-samples=100
    > %(outfile)s
    '''
    P.run()

###################################################################
###################################################################
###################################################################
###################################################################
###################################################################
###################################################################

@follows( loadTranscripts,
          loadTranscriptInformation,
          loadGeneStats,
          loadGeneInformation )
def prepare():
    pass

@follows( makeEffects, loadEffects )
def consequences(): pass

@follows( buildAlleles, loadAlleles,
          summarizeAllelesPerTranscript,
          summarizeAllelesPerGene )
def alleles(): pass

@follows( loadPolyphen, loadPolyphenMap, loadPanther )
def effects(): pass

@follows( loadAnnotations, loadAnnotationsSummary)
def annotations(): pass

@follows( prepare, consequences, effects, alleles, annotations )
def full():
    pass

@files( [ (None, "clone.log" ),] )
def clone( infile, outfile):
    '''clone a pipeline using symbolic links.'''

    src_dir, level = sys.argv[-2:]
    
    if not os.path.exists( src_dir ):
        raise IOError( "directory '%s' does not exist" % src_dir )

    if not os.path.exists( os.path.join( src_dir, "pipeline.ini" )):
        raise IOError( "directory '%s' is not a pipeline" % src_dir )
    
    if level in ("data", ):
        P.execute( "ln -fs %(src_dir)s/*.pileup.* . ")
        P.execute( "ln -fs %(src_dir)s/genome.* . ")
        
        
if __name__== "__main__":
    # P.checkFiles( ("genome.fasta", "genome.idx" ) )
    sys.exit( P.main(sys.argv) )
