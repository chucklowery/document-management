# Project Constitution

***name*** Document Management

## Overview

This project is to create a Document Management System. This product is a Document Management system that will allow complex specifications and collaboration materials to be stored in a text format that machines and robots can read, while also providing enough markup to produce rich documents that people can understand and render into PDF for delivery to 3rd parties that cannot access the repositories.  

Documents will be composed of parts (other documents included in an aggregate) that allow for sections, chapters, tables, and any partial section to be written in an independent document and included in the "main" document. 

Documents and partial documents will be represented as a complex graph, where a document can be referenced and pulled into any other document. While the base level structure will be a tree of folders and documents, documents should be free to reference any document in the entire structure.

Documents should be taggable for searching, and a full representation of a document that combines all partials should be able to be built at any time and stored as a numbered version. The system will keep track of numbered versions. Each numbered version will be available as both a text and a PDF. 

### Vision
1. A comprehensive document management system that allows large collections of working materials to be combined into complete documents that can be versioned and provided to 3rd parties.
2. A document management system that provides all text to be stored in a form that can be easily consumed and indexed by LLMs for reference and integration.
3. A system that is easy to navigate by document authors and researchers that allows for many authors to work at the same time within the document repository/library.
4. An artifact management system that allows for other materials, such as charts, graphs, and tables (CSV/Excel), to be pulled into documentation easily for inclusion as reference materials. Often, the same chart, graph, table, or graphic is needed in multiple documents. The ability to provide both the artifact and the description of the artifact in plain text.
5. A meta system is provided that allows for comments and conversation to occur over a document. This meta system is also stored in text form within adjacent data structures in the repository.

### Objective 
1. Reduce the amount of time required to maintain specifications and complex working materials for projects by providing a repository that can reference all materials.
2. Create a rich ecosystem where both document materials and artifacts all reside along side each other and can be easily encorperated into each other.
3. A system that is both people-readable and maintainable but also allows for robots to work on documents, partials, and other artifacts in a way that multiple people can work together. 

### Guiding Principles
* Text is the canonical representation: The text version is the authoritative, definitive source of truth; artifacts that are published, such as PDFs, will be produced from the text. Artifacts produced such as numbered versions will treated as derivative works and will not be considered as source documents.
* Every generated document is reproducible: Every Document when versioned and produced must have a text version of it as well that is also versioned and number so that it can be reproduced from that text version. Every published document shall be reproducible from an immutable record of its source revision, referenced dependencies, artifacts, rendering configuration, and build-tool version.
* References must be resolvable and traceable.
* No change may silently overwrite another author's work. Concurrent human or automated changes must be detected, preserved, and reconciled explicitly. The system must never silently discard accepted work.
* Published versions are immutable.
* Human-readable and machine-readable representations must remain aligned.
* Artifacts require metadata and accessible textual descriptions.

### Scope
* Document Management
  * Authoring source documents
  * Modifying source documents
  * identifying main documents
  * tagging documents
  * References and dependency management
  * Artifact registration and reuse
  * Search and classification
  * Reusable document composition
  * Preivew of Documennt including inclusion of Partials
  * Review of PDF render would look like
  * Authors must be able to create and format documents without directly editing source syntax
* Artifact Management
  * Artifact registration and reuse
  * Uploading artifacts
  * References and dependency management
  * Artifact registration and reuse
  * Search and classification
  * Reusable document composition
* Diagram Management
  * Uploading new diagrams
  * Example: https://github.com/jgraph/mxgraph
* Publication
  *  Versioned publication
  * PDF rendering
* Meta Management 
  * Document Graph is searchable and taggable 
  * Documents and artifacts can be commented on referencing lines or objects within the artifact
  * All artifacts and documents can be referenced and included into other objects
  * partial documents such as a paragraph or series of lines can be referenced in another document
    * The system shall detect and reject inclusion cycles that prevent deterministic assembly during the assembly phase and the inclusion will not occur.

### Dictionary
***artifact***: is a managed resource that can be referenced by a document, including images, diagrams, datasets, spreadsheets, generated charts, PDFs, and other supporting files. An artifact has a stable identity, metadata, version history, and an accessible textual description where applicable

***Document***: is a text-based managed resource intended to communicate structured information. A document may contain original content, metadata, and references to other documents or artifacts.

***aggragated document*** is a document composed out of text and tokens that idicate where and what other documents should be imported. The document itself is composed of its own descriptions and also inclusion points for other documents or other artifacts such as graphs, images, charts, or tables. 

***Numbered version*** is a snapshot (duplication of a document) of a aggragated document at a specific time captured and numbered so that it can be references as a historical artifact.

***main document*** is a document designated as an entry point into a graph of documents and artifacts. It can be pointed to as a place to start reading from to produce a output artifact such as PDF.

***partial document*** is a document intended primarily for reuse within one or more aggregated documents rather than as a standalone reading entry point.

***repository*** is a storage system that contains documents, artifacts, meta data, and any other resource that could be used in the document and management.

***library*** is a series of main documents that are associated together to produce a meaningful collection of ideas.

***Assembled Document*** are documents produced starting at a main document and pulling in all referenced artifacts. Assembled documents are number versioned documents that are seperate from the working "live" set of documents. 

***Publication*** is a rendered assembled document into a form of a PDF, MS Word Document, other finalized form. 


## Needs
* [Customer Insight Documentation System Vision](./00.01-Interview-Constitution.md)
* [Partial References](./00.02-Partial-References.md)