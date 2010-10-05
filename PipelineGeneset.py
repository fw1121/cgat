'''utility tasks for dealing with ENSEMBL gene sets.

Most of this tasks take a geneset (.gtf.gz) from ENSEMBL
as input.
'''

import sys, re, os, tempfile, collections, shutil, gzip, sqlite3

import Pipeline as P
import Experiment as E
import GTF

try:
    PARAMS = P.getParameters()
except IOError:
    pass

def importRefSeqFromUCSC( infile, outfile, remove_duplicates = True ):
    '''import gene set from UCSC database
    based on refseq mappings.

    Outputs a gtf-formatted file a la ENSEMBL.

    Depending on *remove_duplicates*, duplicate mappings are either
    removed or kept.

    Matches to chr_random are ignored (as does ENSEMBL).

    Note that this approach does not work as a gene set, as refseq maps 
    are not real gene builds and unalignable parts cause
    differences that are not reconcilable.
    '''

    import MySQLdb
    dbhandle = MySQLdb.Connect( host = PARAMS["ucsc_host"],
                                user = PARAMS["ucsc_user"] )
        
    cc = dbhandle.cursor()
    cc.execute( "USE %s " %  PARAMS["ucsc_database"] )
        
    duplicates = set()

    if remove_duplicates:
        cc.execute( """SELECT name, COUNT(*) AS c FROM refGene 
                        WHERE chrom NOT LIKE '%_random'
                        GROUP BY name HAVING c > 1""" )
        duplicates = set( [x[0] for x in cc.fetchall() ] )
        E.info( "removing %i duplicates" % len(duplicates ) )

    # these are forward strand coordinates
    statement = '''
        SELECT gene.name, link.geneName, link.name, gene.name2, product, 
               protAcc, chrom, strand, cdsStart, cdsEnd, 
               exonCount, exonStarts, exonEnds, exonFrames 
        FROM refGene as gene, refLink as link 
        WHERE gene.name = link.mrnaAcc 
              AND chrom NOT LIKE '%_random'
        ORDER by chrom, cdsStart 
        '''
    
    outf = gzip.open(outfile, "w")
    
    cc = dbhandle.cursor()
    cc.execute(statement)

    SQLResult = collections.namedtuple('Result', 
        '''transcript_id, gene_id, gene_name, gene_id2, description,
        protein_id, contig, strand, start, end, 
        nexons, starts, ends, frames''')

    counts = E.Counter()
    counts.duplicates = len(duplicates)

    for r in map( SQLResult._make, cc.fetchall() ):

        if r.transcript_id in duplicates: continue

        starts = map( int, r.starts.split(",")[:-1])
        ends = map( int, r.ends.split(",")[:-1])
        frames = map( int, r.frames.split(",")[:-1])

        gtf = GTF.Entry()
        gtf.contig = r.contig
        gtf.source = "protein_coding"
        gtf.strand = r.strand
        gtf.gene_id = r.gene_id
        gtf.transcript_id = r.transcript_id
        gtf.addAttribute( "protein_id", r.protein_id )
        gtf.addAttribute( "transcript_name", r.transcript_id )
        gtf.addAttribute( "gene_name", r.gene_name )

        assert len(starts) == len(ends) == len(frames)

        if gtf.strand == "-":
            starts.reverse()
            ends.reverse()
            frames.reverse()

        counts.transcripts += 1
        i = 0
        for start, end, frame in zip( starts, ends, frames ):
            gtf.feature = "exon"
            counts.exons += 1
            i += 1
            gtf.addAttribute("exon_number", i)
            # frame of utr exons is set to -1 in UCSC
            gtf.start, gtf.end, gtf.frame = start, end, "."
            outf.write( "%s\n" % str(gtf))
            
            cds_start, cds_end = max( r.start, start), min( r.end, end)
            if cds_start >= cds_end: 
                # UTR exons have no CDS
                # do not expect any in UCSC
                continue
            gtf.feature = "CDS"
            # invert the frame
            frame = (3 - frame % 3 ) % 3
            gtf.start, gtf.end, gtf.frame = cds_start, cds_end, frame
            outf.write( "%s\n" % str(gtf))

    outf.close()
    
    E.info("%s" % str(counts))

############################################################
############################################################
############################################################
def buildGeneRegions( infile, outfile, only_proteincoding = False ):
    '''annotate genomic regions with reference gene set.

    *infile* is an ENSEMBL gtf file.

    In case of overlapping genes, only take the longest (in genomic coordinates).

    Genes not on UCSC contigs are removed.

    Only considers protein coding genes, if ``only_proteincoding`` is set.
    '''

    to_cluster = True

    if only_proteincoding: filter_cmd = ''' awk '$2 == "protein_coding"' '''
    else: filter_cmd = "cat"

    statement = """
            gunzip 
            < %(infile)s
            | %(filter_cmd)s 
            | python %(scriptsdir)s/gtf2gtf.py --sort=gene
            | python %(scriptsdir)s/gff2gff.py --sanitize=genome --skip-missing --genome-file=%(genome)s 
            | python %(scriptsdir)s/gtf2gtf.py --merge-exons --with-utr --log=%(outfile)s.log 
            | python %(scriptsdir)s/gtf2gtf.py --filter=longest-gene --log=%(outfile)s.log 
            | python %(scriptsdir)s/gtf2gtf.py --sort=position
            | python %(scriptsdir)s/gtf2gff.py --genome-file=%(genome)s --log=%(outfile)s.log --flank=%(geneset_flank)s 
            | gzip 
            > %(outfile)s
        """
    P.run()

############################################################
############################################################
############################################################
def buildProteinCodingGenes( infile, outfile ):
    '''build a collection of exons from the protein-coding
    section of the ENSEMBL gene set. The exons include both CDS
    and UTR.

    *infile* is an ENSEMBL gtf file.

    The set is filtered in the same way as in :meth:`buildGeneRegions`.

    '''

    to_cluster = True

    # sort by contig+gene, as in refseq gene sets, genes on
    # chr_random might contain the same identifier as on chr
    # and hence merging will fail.
    # --permit-duplicates is set so that these cases will be
    # assigned new merged gene ids.
    statement = """gunzip 
            < %(infile)s 
            | awk '$2 == "protein_coding"' 
            | python %(scriptsdir)s/gtf2gtf.py --sort=contig+gene
            | python %(scriptsdir)s/gff2gff.py --sanitize=genome --skip-missing --genome-file=%(genome)s 
            | python %(scriptsdir)s/gtf2gtf.py --merge-exons --permit-duplicates --log=%(outfile)s.log 
            | python %(scriptsdir)s/gtf2gtf.py --filter=longest-gene --log=%(outfile)s.log 
            | awk '$3 == "exon"' 
            | python %(scriptsdir)s/gtf2gtf.py --set-transcript-to-gene --log=%(outfile)s.log 
            | python %(scriptsdir)s/gtf2gtf.py --sort=gene
            | gzip
            > %(outfile)s
        """
    P.run()

############################################################
############################################################
############################################################
def loadGeneInformation( infile, outfile, only_proteincoding = False ):
    '''load gene information gleaned from the attributes
    in the gene set gtf file.

    *infile* is an ENSEMBL gtf file.

    '''

    table = outfile[:-len(".load")]

    if only_proteincoding: filter_cmd = ''' awk '$2 == "protein_coding"' '''
    else: filter_cmd = "cat"

    statement = '''
    gunzip < %(infile)s 
    | %(filter_cmd)s 
    | python %(scriptsdir)s/gtf2gtf.py --sort=gene
    | python %(scriptsdir)s/gtf2tab.py --full --only-attributes -v 0
    | python %(toolsdir)s/csv_cut.py --remove exon_number transcript_id transcript_name protein_id
    | hsort 1 | uniq 
    | csv2db.py %(csv2db_options)s 
              --index=gene_id 
              --index=gene_name 
              --map=gene_name:str 
              --table=%(table)s 
    > %(outfile)s'''

    P.run()

############################################################
############################################################
############################################################
def loadTranscriptInformation( infile, outfile,
                                 only_proteincoding = False ):
                                 
    '''load the transcript set.

    *infile* is an ENSEMBL gtf file.
    '''
    to_cluster = True

    table = outfile[:-len(".load")]

    if only_proteincoding: filter_cmd = ''' awk '$2 == "protein_coding"' '''
    else: filter_cmd = "cat"

    statement = '''gunzip 
    < %(infile)s 
    | %(filter_cmd)s 
    | awk '$3 == "CDS"' 
    | python %(scriptsdir)s/gtf2gtf.py --sort=gene
    | python %(scriptsdir)s/gtf2tab.py --full --only-attributes -v 0
    | python %(toolsdir)s/csv_cut.py --remove exon_number 
    | hsort 1 | uniq 
    | csv2db.py %(csv2db_options)s 
              --index=transcript_id 
              --index=gene_id 
              --index=protein_id 
              --index=gene_name 
              --map=transcript_name:str 
              --map=gene_name:str 
              --table=%(table)s 
    > %(outfile)s'''
    P.run()

############################################################
############################################################
############################################################
def buildCDNAFasta( infile, outfile ):
    '''load ENSEMBL cdna FASTA file
    
    *infile* is an ENSEMBL cdna file.
    '''
    dbname = outfile[:-len(".fasta")]

    statement = '''gunzip 
    < %(infile)s
    | perl -p -e 'if ("^>") { s/ .*//};'
    | python %(scriptsdir)s/index_fasta.py
    %(dbname)s - 
    > %(dbname)s.log
    '''

    P.run()

############################################################
############################################################
############################################################
def buildPeptideFasta( infile, outfile ):
    '''load ENSEMBL peptide file

    *infile* is an ENSEMBL .pep.all.fa.gz file.
    '''
    dbname = outfile[:-len(".fasta")]

    statement = '''gunzip 
    < %(infile)s
    | perl -p -e 'if ("^>") { s/ .*//};'
    | python %(scriptsdir)s/index_fasta.py
    %(dbname)s - 
    > %(dbname)s.log
    '''

    P.run()

############################################################
############################################################
############################################################
def buildCDSFasta( infile, outfile ):
    '''load ENSEMBL cdna FASTA file
    
    *infile* is an ENSEMBL cdna file.
    '''

    dbname = outfile[:-len(".fasta")]
    # infile_peptides, infile_cdnas = infiles

    statement = '''gunzip < %(infile)s
    | python %(scriptsdir)s/gff2fasta.py
        --is-gtf 
        --genome=%(genome)s
    | python %(scriptsdir)s/index_fasta.py
    %(dbname)s --force - 
    > %(dbname)s.log
    '''
    P.run()
    return

    tmpfile = P.getTempFile(".")

    dbhandle = sqlite3.connect( PARAMS["database"] )
    cc = dbhandle.cursor()
    tmpfile.write("protein_id\ttranscript_id\n")
    tmpfile.write( "\n".join( 
            [ "%s\t%s" % x for x in \
                  cc.execute("SELECT DISTINCT protein_id,transcript_id FROM transcript_info") ]))
    tmpfile.write( "\n" )

    tmpfile.close()
    
    tmpfilename = tmpfile.name


    statement = '''
    python %(scriptsdir)s/peptides2cds.py 
           --peptides=%(infile_peptides)s
           --cdnas=%(infile_cdnas)s
           --map=%(tmpfilename)s
           --output-format=fasta
           --log=%(outfile)s.log
    | python %(scriptsdir)s/index_fasta.py
    %(dbname)s --force - 
    > %(dbname)s.log
    '''

    P.run()
    os.unlink( tmpfilename )

############################################################
############################################################
############################################################
def loadGeneStats( infile, outfile ):
    '''load gene statistics to database.

    The *infile* is the *outfile* from :meth:`buildGenes`
    '''

    # do not run on cluster - 32/64 bit incompatible.
    # to_cluster = True

    table = outfile[:-len(".load")]

    statement = '''
    gunzip < %(infile)s |\
    python %(scriptsdir)s/gtf2table.py \
          --log=%(outfile)s.log \
          --genome=%(genome)s \
          --counter=position \
          --counter=length \
          --counter=composition-na |\
    csv2db.py %(csv2db_options)s \
              --index=gene_id \
              --map=gene_id:str \
              --table=%(table)s \
    > %(outfile)s'''
    P.run()

############################################################
############################################################
############################################################
def buildProteinCodingTranscripts( infile, outfile ):
    '''build a collection of transcripts from the protein-coding
    section of the ENSEMBL gene set.

    Only CDS are used.
    '''

    to_cluster = True

    statement = '''
    gunzip < %(infile)s 
    | awk '$2 == "protein_coding"' 
    | awk '$3 == "CDS"' 
    | python %(scriptsdir)s/gff2gff.py --sanitize=genome --skip-missing --genome-file=%(genome)s --log=%(outfile)s.log 
    | python %(scriptsdir)s/gtf2gtf.py --remove-duplicates=gene --log=%(outfile)s.log 
    | gzip > %(outfile)s
    '''
    P.run()

############################################################
############################################################
############################################################
def loadTranscripts( infile, outfile ):
    '''load the transcript set.'''
    table = outfile[:-len(".load")]
    
    statement = '''
    gunzip < %(infile)s 
    | python %(scriptsdir)s/gtf2tab.py
    | csv2db.py %(csv2db_options)s 
              --index=transcript_id 
              --index=gene_id 
              --table=%(table)s 
    > %(outfile)s'''
    P.run()

############################################################
############################################################
############################################################
def loadTranscriptStats( infile, outfile ):
    '''load gene statistics to database.

    The *infile* is the *outfile* from :meth:`buildTranscripts`
    '''

    to_cluster = True

    table = outfile[:-len(".load")]

    statement = '''
    gunzip < %(infile)s |\
    python %(scriptsdir)s/gtf2table.py \
          --log=%(outfile)s.log \
          --genome=%(genome)s \
          --reporter=transcripts \
          --counter=position \
          --counter=length \
          --counter=composition-na |\
    csv2db.py %(csv2db_options)s \
              --index=gene_id \
              --map=gene_id:str \
              --table=%(table)s \
    > %(outfile)s'''

    P.run()

############################################################
############################################################
############################################################
def loadProteinStats( infile, outfile ):
    '''load protein statistics to database.

    The *infile* is an ENSEMBL peptide file.
    '''

    to_cluster = True

    table = outfile[:-len(".load")]

    statement = '''
    gunzip < %(infile)s |
    python %(scriptsdir)s/fasta2properties.py 
          --log=%(outfile)s
          --type=aa 
          --section=length 
          --section=hid 
          --section=aa 
          --regex-identifier="(\S+)" |
    sed "s/^id/protein_id/" |
    csv2db.py %(csv2db_options)s 
              --index=protein_id 
              --map=protein_id:str 
              --table=%(table)s 
    > %(outfile)s'''

    P.run()

############################################################
############################################################
############################################################
def buildPromotorRegions( infile, outfile ):
    '''annotate promotor regions from reference gene set.'''
    statement = """
        gunzip < %(infile)s |\
        python %(scriptsdir)s/gff2gff.py --sanitize=genome --skip-missing --genome-file=%(genome)s --log=%(outfile)s.log |\
        python %(scriptsdir)s/gtf2gff.py --method=promotors --promotor=%(promotor_size)s \
                              --genome-file=%(genome)s --log=%(outfile)s.log 
        | gzip 
        > %(outfile)s
    """
    P.run()

############################################################
############################################################
############################################################
def buildTSSRegions( infile, outfile ):
    '''annotate transcription start sites from reference gene set.

    Similar to promotors, except that the witdth is set to 1.
    '''
    statement = """
        gunzip < %(infile)s |\
        python %(scriptsdir)s/gff2gff.py --sanitize=genome --skip-missing --genome-file=%(genome)s --log=%(outfile)s.log |\
        python %(scriptsdir)s/gtf2gff.py --method=promotors --promotor=1 --genome-file=%(genome)s --log=%(outfile)s.log > %(outfile)s
    """
    P.run()

############################################################
############################################################
############################################################
def buildOverlapWithEnsembl( infile, outfile, filename_bed ):
    '''compute overlap of genes in ``infile`` with intervals
    in ``filename_bed`` and load into database.

    If ``filename_bed`` has multiple tracks the overlap will
    be computed for each track separately.

    ``infile`` is the output from :meth:`buildGenes`.
    '''

    to_cluster = True
    statement = '''gunzip 
        < %(infile)s 
        | python %(scriptsdir)s/gtf2gtf.py --merge-transcripts 
        | python %(scriptsdir)s/gff2bed.py --is-gtf 
        | python %(scriptsdir)s/bed2graph.py 
            --output=name 
            --log=%(outfile)s.log 
            - %(filename_bed)s 
        > %(outfile)s
    '''
    P.run()

############################################################
############################################################
############################################################
def compareGeneSets( infiles, outfile ):
    '''compute overlap of genes, exons and transcripts in ``infiles`` 

    ``infiles`` are protein coding gene sets.
    '''

    infiles = " ".join(infiles)
    to_cluster = True
    statement = '''
        python %(scriptsdir)s/diff_gtfs.py 
        %(infiles)s
    > %(outfile)s
    '''
    P.run()
